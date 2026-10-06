"""Opsi scheduling decision layer.

`OpsiScheduling` is the Operation Siren *internal* scheduler: it decides **what
to do now**, while each sub-task decides **how to do it**. The design follows
AzurPilot's state-driven scheduling, mapped onto the Alas scheduler:

- one round = one decision + one sub-task round, then back to the decision
  (`run_opsi_scheduler_once()`), never "task A finished -> always start task B";
- coin replenish tasks (Stronghold / Obscure / Abyssal / Meowfficer) are all
  proxied by this layer, ordered by ``OpsiScheduling_TaskPriority``; the first
  candidate with content runs exactly one round and returns;
- "no content" is not a failure: it postpones that candidate and the dispatcher
  tries the next one;
- sub-task delays are owned by the scheduler through the task context, so a
  proxied sub-task can never move its own schedule.
"""

from datetime import datetime, timedelta

from module.config.utils import (
    get_nearest_weekday_date,
    get_os_next_reset,
    get_os_reset_remain,
    get_server_next_update,
)
from module.exception import GameStuckError, GameTooManyClickError, RequestHumanTakeover
from module.logger import logger
from module.os_handler.action_point import ActionPointLimit
from module.os.map import OSMap
from module.os.opsi_notify import (
    clear_coin_task_push_state,
    notify_action_point_change,
    notify_ap_insufficient,
    notify_coin_task_disabled,
    notify_coin_task_proxy,
    notify_coins_ap_insufficient,
)
from module.os.tasks.task_context import (
    OpsiNoContent,
    OpsiStatus,
    OpsiTaskResult,
    opsi_task_context,
    pop_opsi_no_content,
)


def decide_resource_action(
        yellow_coins,
        total_ap,
        coin_preserve,
        coin_return_threshold,
        ap_preserve,
        meow_ap_preserve,
        coin_target_mode,
        coin_replenish_active,
        ap_replenish_active=False,
):
    """Choose the next Operation Siren action from current coin and AP totals.

    Returns:
        tuple[str, bool, bool]: (action, coin_replenish_active, ap_replenish_active)
    """
    coin_preserve = max(int(coin_preserve), 0)
    coin_return_threshold = max(int(coin_return_threshold), 0)
    ap_preserve = max(int(ap_preserve), 0)
    meow_ap_preserve = max(int(meow_ap_preserve), 0)

    if total_ap <= ap_preserve:
        return 'wait', coin_replenish_active, ap_replenish_active

    if coin_target_mode:
        coin_replenish_active = coin_replenish_active or yellow_coins < coin_preserve
        coin_target = coin_preserve + coin_return_threshold
        if coin_replenish_active and yellow_coins < coin_target:
            if total_ap <= meow_ap_preserve:
                return 'wait', True, ap_replenish_active
            return 'meow', True, ap_replenish_active
        return 'cl1', False, ap_replenish_active

    # Action point scheduling (non coin-target): once coins drop below the
    # preserve value, keep replenishing coins until AP drops to the meow preserve
    # (hysteresis), matching AzurPilot's ap_replenish_active.
    if yellow_coins < coin_preserve or ap_replenish_active:
        ap_replenish_active = True
        if total_ap <= meow_ap_preserve:
            ap_replenish_active = False
            if yellow_coins < coin_preserve:
                return 'wait', coin_replenish_active, False
            return 'cl1', coin_replenish_active, False
        return 'meow', coin_replenish_active, True

    return 'cl1', coin_replenish_active, ap_replenish_active


class SchedulingMode:
    """State of the Opsi scheduling decision layer."""

    NORMAL = 'NORMAL'
    COIN_TARGET = 'COIN_TARGET'
    ACTION_POINT = 'ACTION_POINT'
    OPSI_EXPLORE_BLOCKED = 'OPSI_EXPLORE_BLOCKED'
    WAITING = 'WAITING'
    NO_TASK = 'NO_TASK'


CONFIG_PATH_SMART_STATE = 'OpsiScheduling.Storage.Storage'

STATE_KEY_SCHEDULING_MODE = 'SchedulingMode'
STATE_KEY_COIN_REPLENISH = 'CoinReplenishActive'
STATE_KEY_AP_REPLENISH = 'ApReplenishActive'
STATE_KEY_STRONGHOLD_NEXT_CHECK = 'StrongholdNextCheck'
STATE_KEY_OBSCURE_NEXT_CHECK = 'ObscureNextCheck'
STATE_KEY_ABYSSAL_NEXT_CHECK = 'AbyssalNextCheck'

TASK_NAME_HAZARD1_LEVELING = 'OpsiHazard1Leveling'
TASK_NAME_MEOWFFICER_FARMING = 'OpsiMeowfficerFarming'
TASK_NAME_OBSCURE = 'OpsiObscure'
TASK_NAME_ABYSSAL = 'OpsiAbyssal'
TASK_NAME_STRONGHOLD = 'OpsiStronghold'

# Coin replenish candidates, in the order AzurPilot ships as the default.
COIN_TASK_NAMES = (
    TASK_NAME_STRONGHOLD,
    TASK_NAME_OBSCURE,
    TASK_NAME_ABYSSAL,
    TASK_NAME_MEOWFFICER_FARMING,
)
DEFAULT_TASK_PRIORITY = ' > '.join(COIN_TASK_NAMES)

COIN_TASK_ENABLE_KEYS = {
    TASK_NAME_STRONGHOLD: 'EnableStronghold',
    TASK_NAME_OBSCURE: 'EnableObscure',
    TASK_NAME_ABYSSAL: 'EnableAbyssal',
    TASK_NAME_MEOWFFICER_FARMING: 'EnableMeowfficerFarming',
}
COIN_TASK_POSTPONE_KEYS = {
    TASK_NAME_STRONGHOLD: STATE_KEY_STRONGHOLD_NEXT_CHECK,
    TASK_NAME_OBSCURE: STATE_KEY_OBSCURE_NEXT_CHECK,
    TASK_NAME_ABYSSAL: STATE_KEY_ABYSSAL_NEXT_CHECK,
}
# One-round coin proxies are short: letting a switch end them mid-round only
# complicates the delay bookkeeping, so switching is disabled for them.
# Meowfficer keeps its previous behaviour (switch allowed).
COIN_TASK_DISABLE_TASK_SWITCH = {
    TASK_NAME_STRONGHOLD: True,
    TASK_NAME_OBSCURE: True,
    TASK_NAME_ABYSSAL: True,
    TASK_NAME_MEOWFFICER_FARMING: False,
}
# Near the monthly reset the tasks themselves delay 2.5 hours instead of a day.
RESET_NEAR_DELAY_MINUTES = 150


class OpsiScheduling(OSMap):
    # 塞壬要塞清空后的检查缓冲：要塞刷新时间（周一 0 点 / 每月 1 日）再加 2 小时，
    # 避免卡着刷新时刻反复检查（对齐 AP master 的 RESET_CHECK_GRACE）。
    RESET_CHECK_GRACE = timedelta(hours=2)

    # ------------------------------------------------------------------ state

    def _get_smart_state(self) -> dict:
        state = self.config.cross_get(CONFIG_PATH_SMART_STATE, default={})
        return dict(state) if isinstance(state, dict) else {}

    def _save_smart_state(self, state: dict):
        self.config.cross_set(CONFIG_PATH_SMART_STATE, state)

    def _sync_scheduling_mode(self, state: dict, mode: str) -> bool:
        """Record the mode and drop the previous mode's temporary state.

        A mode switch must never let stale state leak into the new mode, e.g.
        COIN_REPLENISH -> NORMAL must clear the coin replenish bookkeeping.
        """
        previous = state.get(STATE_KEY_SCHEDULING_MODE)
        if previous == mode:
            return False
        if previous in (SchedulingMode.COIN_TARGET, SchedulingMode.ACTION_POINT):
            state.pop(STATE_KEY_COIN_REPLENISH, None)
            state.pop(STATE_KEY_AP_REPLENISH, None)
            logger.info(f'[大世界-调度] 模式切换 {previous} -> {mode}，清理旧模式状态')
        state[STATE_KEY_SCHEDULING_MODE] = mode
        return True

    def _decide_scheduling_mode(self, action, coin_target_mode) -> str:
        if action == 'wait':
            return SchedulingMode.WAITING
        if action == 'meow':
            return SchedulingMode.COIN_TARGET if coin_target_mode else SchedulingMode.ACTION_POINT
        return SchedulingMode.NORMAL

    # --------------------------------------------------------- task selection

    def _get_task_priority(self) -> list:
        raw = self.config.cross_get(
            'OpsiScheduling.OpsiScheduling.TaskPriority', default=DEFAULT_TASK_PRIORITY)
        if not isinstance(raw, str) or not raw.strip():
            raw = DEFAULT_TASK_PRIORITY
        order = [name.strip() for name in raw.split('>') if name.strip()]
        # Anything not listed keeps the default relative order, at the end.
        for name in COIN_TASK_NAMES:
            if name not in order:
                order.append(name)
        return order

    def _is_coin_task_enabled(self, task_name) -> bool:
        key = COIN_TASK_ENABLE_KEYS.get(task_name)
        if key is None:
            return False
        return bool(self.config.cross_get(
            f'OpsiScheduling.OpsiScheduling.{key}', default=True))

    def _is_hazard1_enabled(self) -> bool:
        return bool(self.config.cross_get(
            'OpsiScheduling.OpsiScheduling.EnableHazard1Leveling', default=True))

    def _get_enabled_coin_tasks(self) -> list:
        """Enabled coin replenish tasks, ordered by TaskPriority."""
        priority = self._get_task_priority()
        enabled = [name for name in COIN_TASK_NAMES if self._is_coin_task_enabled(name)]
        return sorted(enabled, key=lambda name: priority.index(name))

    def _get_coin_task_action_point_preserve(self) -> int:
        """Total AP a coin replenish task needs before it may start.

        The scheduler's own line rules, and the meowfficer line is only a
        fallback for when that line is switched off (0) -- this is AzurPilot's
        precedence (`_get_coin_task_action_point_preserve`).
        """
        ap_preserve = max(int(self.config.OpsiScheduling_ActionPointPreserve), 0)
        if ap_preserve > 0:
            return ap_preserve
        return max(int(self.config.OpsiScheduling_MeowfficerActionPointPreserve), 0)

    def _get_scheduled_meow_ap_preserve(self) -> int:
        """This round's coin-task AP line, still lowered by the month-end limit."""
        return min(
            self.get_action_point_limit(),
            self._get_coin_task_action_point_preserve(),
            2000,
        )

    def _get_coin_replenish_target(self) -> int:
        """本轮补黄币的目标值：目标模式按 保留值 + 回补阈值，否则就是保留值。"""
        target = int(self.config.OpsiScheduling_OperationCoinsPreserve)
        if self.config.OpsiScheduling_UseSmartSchedulingOperationCoinsPreserve:
            target += int(self.config.OpsiScheduling_OperationCoinsReturnThreshold)
        return target

    # ------------------------------------------------------------- postpone

    def _get_next_stronghold_check_time(self):
        """下次塞壬要塞可能刷新的时间。

        要塞数量有限：每周（服务器周一 0 点）刷新 1 个，每月 1 日随大世界重置
        再刷新 1 个。清除干净后要等这两个时间点才会有新的，取其中较早的一个。

        Returns:
            datetime.datetime: 下次要塞刷新时间（本地时间，含 2 小时缓冲）。
        """
        next_weekly = get_nearest_weekday_date(0)
        next_monthly = get_os_next_reset()
        return min(next_weekly, next_monthly) + self.RESET_CHECK_GRACE

    def _postpone_coin_task_check(self, task_name, reason=''):
        """Remember that this candidate was cleared, so it is skipped for a while."""
        state_key = COIN_TASK_POSTPONE_KEYS.get(task_name)
        if state_key is None:
            return
        if task_name == TASK_NAME_STRONGHOLD:
            # 塞壬要塞打完就没了，继续搜索只是反复遍历全球地图：
            # 推迟到下次要塞刷新（每周一 / 每月 1 日取较早），对齐 AP master。
            next_check = self._get_next_stronghold_check_time()
        elif get_os_reset_remain() <= 0:
            next_check = datetime.now() + timedelta(minutes=RESET_NEAR_DELAY_MINUTES)
        else:
            next_check = get_server_next_update('00:00')
        state = self._get_smart_state()
        state[state_key] = next_check.isoformat()
        self._save_smart_state(state)
        logger.info(f'[大世界-调度] {task_name}: 已清空（{reason or "no content"}），下次检查 {next_check}')

    def _get_coin_task_postpone(self, task_name):
        state_key = COIN_TASK_POSTPONE_KEYS.get(task_name)
        if state_key is None:
            return None
        state = self._get_smart_state()
        raw = state.get(state_key)
        if not raw:
            return None
        try:
            next_check = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            next_check = None
        if next_check is None or datetime.now() >= next_check:
            state.pop(state_key, None)
            self._save_smart_state(state)
            return None
        return next_check

    # ------------------------------------------------------------ execution

    def _get_coin_task_handler(self, task_name):
        return {
            TASK_NAME_MEOWFFICER_FARMING: self.os_meowfficer_farming,
            TASK_NAME_OBSCURE: self.clear_obscure,
            TASK_NAME_ABYSSAL: self.clear_abyssal,
            TASK_NAME_STRONGHOLD: self.clear_stronghold,
        }.get(task_name)

    def _run_scheduled_coin_task_once(self, task_name, ap_preserve, fresh_ap=None) -> OpsiTaskResult:
        """Proxy one round of a coin replenish task and report its result."""
        handler = self._get_coin_task_handler(task_name)
        if handler is None:
            logger.error(f'[大世界-调度] 不支持代理执行黄币补充任务: {task_name}')
            return OpsiTaskResult(OpsiStatus.FAILED, task=task_name, reason='unsupported task')

        logger.info(f'[大世界-调度] TASK_START {task_name}')
        pop_opsi_no_content(self.config)
        if task_name != TASK_NAME_MEOWFFICER_FARMING:
            # 其它补币任务要自行导航地图，先把决策暂留的行动力面板关掉
            self._close_scheduling_action_point()
        try:
            with opsi_task_context(
                self.config,
                task_name,
                disable_task_switch=COIN_TASK_DISABLE_TASK_SWITCH.get(task_name, True),
            ):
                if task_name == TASK_NAME_MEOWFFICER_FARMING:
                    # 耄耋相接与决策共用同一个行动力面板：本轮决策刚用新鲜读数
                    # 验证过总行动力高于短猫保留线，ap_checked=True 让短猫跳过
                    # 那次重复的前置检查（否则一轮里多一组 REMAIN_OS + CANCEL）。
                    # ap_preserve 一起传下去：否则短猫按自己的配置另算保留线，
                    # 要么被 ActionPointLimit 打死整轮调度，要么把行动力吃穿到 0。
                    handler(ap_preserve=ap_preserve, fresh_ap=fresh_ap, ap_checked=True)
                else:
                    # 这三个任务不自己设保留值，代跑期间统一用本轮调度阈值。
                    with self.config.temporary(OS_ACTION_POINT_PRESERVE=int(ap_preserve)):
                        handler()
        except OpsiNoContent as e:
            self._postpone_coin_task_check(task_name, str(e))
            return OpsiTaskResult(OpsiStatus.NO_CONTENT, task=task_name, reason=str(e))
        except ActionPointLimit as e:
            if (task_name == TASK_NAME_MEOWFFICER_FARMING and int(ap_preserve) > 0
                    and getattr(e, 'preserve', None) == int(ap_preserve)):
                # 打到本轮调度线属于正常收尾，交回决策重新判断，不记成「行动力不足」。
                logger.info(
                    f'[大世界-调度] {task_name} 达到本轮保留线，返回调度决策: '
                    f'total={e.total} preserve={e.preserve}')
                return OpsiTaskResult(
                    OpsiStatus.SUCCESS, task=task_name, reason='reached scheduling AP line')
            # 行动力打到保留线：优雅推迟到服务器刷新，而不是以任务报错收场
            # （对齐 AP master 的调度器兜底）。
            logger.warning(
                f'[大世界-调度] {task_name} 行动力不足（达到保留线），推迟到服务器刷新: '
                f'total={e.total} preserve={e.preserve}')
            notify_ap_insufficient(self, e.total, e.preserve)
            self.config.task_delay(server_update=True)
            self.config.task_stop()
            return OpsiTaskResult(OpsiStatus.FAILED, task=task_name, reason='action point limit')

        no_content_task = pop_opsi_no_content(self.config)
        if no_content_task == task_name:
            self._postpone_coin_task_check(task_name, 'no content')
            return OpsiTaskResult(OpsiStatus.NO_CONTENT, task=task_name, reason='no content')
        return OpsiTaskResult(OpsiStatus.SUCCESS, task=task_name, map_changed=True)

    def _dispatch_coin_task(self, yellow_coins, total_ap, meow_ap_preserve, current_ap) -> OpsiTaskResult:
        """Try enabled coin tasks in TaskPriority order, run one round of the first
        candidate that has content, and report its result.

        A candidate with nothing to do is postponed (never treated as a failure)
        and the next candidate is tried. When every candidate is postponed or has
        no content, NO_CONTENT is returned so the caller can delay this round.
        """
        candidates = self._get_enabled_coin_tasks()
        if not candidates:
            logger.error('[大世界-调度] 没有启用任何黄币补充任务')
            notify_coin_task_disabled(self)
            return OpsiTaskResult(OpsiStatus.NO_TASK, reason='no coin task enabled')

        logger.info(f'[大世界-调度] 黄币补充候选: {"、".join(candidates)}')
        skipped = []
        for task_name in candidates:
            postpone_until = self._get_coin_task_postpone(task_name)
            if postpone_until is not None:
                logger.info(f'[大世界-调度] {task_name}: DELAYED until {postpone_until}')
                skipped.append(task_name)
                continue

            logger.info(f'[大世界-调度] 选择任务: {task_name}（原因: COIN_REPLENISH）')
            result = self._run_scheduled_coin_task_once(
                task_name, meow_ap_preserve,
                fresh_ap=(total_ap, current_ap) if current_ap is not None else None,
            )
            if result.executed:
                notify_coin_task_proxy(
                    self, yellow_coins, total_ap,
                    self._get_coin_replenish_target(), meow_ap_preserve, task_name)
                return result
            skipped.append(task_name)

        logger.info(f'[大世界-调度] 黄币补充任务均无可执行内容: {"、".join(skipped)}')
        return OpsiTaskResult(OpsiStatus.NO_CONTENT, reason='no coin task has content')

    def handle_first_auto_search(self, run):
        """由智能调度决定是否补跑 os_init 阶段跳过的首次自律寻敌。

        Args:
            run (bool): True 补跑一次首次自律寻敌；False 跳过。
        """
        if not getattr(self, '_smart_scheduling_first_auto_search_pending', False):
            return
        self._smart_scheduling_first_auto_search_pending = False

        if not run:
            logger.info('[大世界-调度] 跳过 os_init 的首次自律寻敌（子任务自带清场流程）')
            return

        logger.info('[大世界-调度] 补跑 os_init 的首次自律寻敌')
        self.run_first_auto_search()

    def _execute_hazard1_leveling_once(self, yellow_coins, total_ap, current_ap) -> OpsiTaskResult:
        """Proxy one round of侵蚀 1 练级, which is the default NORMAL action."""
        logger.info(f'[大世界-调度] 选择任务: {TASK_NAME_HAZARD1_LEVELING}（原因: 黄币充足）')
        logger.info(f'[大世界-调度] TASK_START {TASK_NAME_HAZARD1_LEVELING}')
        # 黄币补够、回到侵蚀 1 练级：清掉补币推送的去重状态，
        # 否则下次再进入补币阶段时同一个任务不会再推（对齐 AP 的通知清理）。
        clear_coin_task_push_state(self)
        # 侵蚀 1 练级自带完整的清场流程（计划作战 + clear_question + map_rescan），
        # 所以 os_init 挂起的首次自律寻敌在这里必须跳过：一旦先跑了自律，
        # 当前海域会被打空，随后的计划作战无目标可打，地图事件也就失去了
        # 被 map_rescan 处理的机会（这正是「自律完直接进计划作战、事件被漏」的原因）。
        # AzurPilot 同样在代理侵蚀 1 前 handle_first_auto_search(run=False)。
        self.handle_first_auto_search(run=False)
        try:
            with opsi_task_context(self.config, TASK_NAME_HAZARD1_LEVELING, disable_task_switch=False):
                # 把决策刚读到的行动力交给子任务，它在同一个面板里完成开工检查
                self.os_hazard1_leveling(fresh_ap=(total_ap, current_ap))
        except OpsiNoContent as e:
            return OpsiTaskResult(OpsiStatus.NO_CONTENT, task=TASK_NAME_HAZARD1_LEVELING, reason=str(e))
        return OpsiTaskResult(OpsiStatus.SUCCESS, task=TASK_NAME_HAZARD1_LEVELING, map_changed=True)

    def _handle_opsi_task_result(self, result: OpsiTaskResult):
        logger.info(f'[大世界-调度] TASK_END {result}')
        logger.info(
            f'[大世界-调度] 子任务 {result.task or "-"} 返回 {result.status}'
            f'（map_changed={result.map_changed}）')
        logger.info('[大世界-调度] 重新进入调度决策')

    def _delay_to_server_update(self, reason=''):
        logger.info(f'[大世界-调度] 延迟到服务器刷新（{reason or "no reason"}）')
        self.config.task_delay(server_update=True)

    def _delay_for_opsi_explore(self) -> bool:
        """Let 开荒 have the world to itself: no scheduling while it runs."""
        if not self.is_in_opsi_explore():
            return False
        logger.info('[大世界-调度] OpsiExplore 正在运行，智能调度让路')
        state = self._get_smart_state()
        if self._sync_scheduling_mode(state, SchedulingMode.OPSI_EXPLORE_BLOCKED):
            self._save_smart_state(state)
        self._delay_to_server_update('每月开荒正在运行')
        self.config.task_stop()
        return True

    # ------------------------------------------------- 行动点面板的生命周期管理
    #
    # 决策要读一次行动力，子任务开工还要再读一次。为了省掉「关窗 → 重开 → 重读」
    # 这一趟往返，决策读完先**不关**面板，等确定子任务之后在同一面板里关掉或直接
    # 完成开工补充（对齐 AzurPilot 的 _get_scheduling_action_point / _prepare_...）。
    # 面板暂留期间任何出口都必须保证关窗，因此调用方用 try/finally 兜住。

    # 决策暂留的行动力面板是否还开着，以及当时读取用的含箱口径。
    _scheduling_ap_panel_open = False
    _scheduling_ap_box_use = None

    def _close_scheduling_action_point(self):
        """关闭决策期间暂留的行动力面板；没有暂留时什么都不做。"""
        if getattr(self, '_scheduling_ap_panel_open', False):
            self._scheduling_ap_panel_open = False
            self.action_point_quit()

    def _get_scheduling_action_point(self, keep_open=False):
        """读取智能调度决策所需的行动力。

        Args:
            keep_open (bool): 决策期间保留面板，确定子任务后再关闭或合并开工补充。

        Returns:
            tuple[int, int]: (弹窗口径总行动力, 当前真实行动力)。

        Pages:
            in: page_os
            out: keep_open=True 时为 ACTION_POINT_USE，否则为 page_os
        """
        self._close_scheduling_action_point()
        self.action_point_enter()
        self.action_point_safe_get()
        if keep_open:
            self._scheduling_ap_panel_open = True
            self._scheduling_ap_box_use = self.config.OS_ACTION_POINT_BOX_USE
        else:
            self.action_point_quit()
        notify_action_point_change(self)
        return (
            int(getattr(self, '_action_point_total', 0) or 0),
            int(getattr(self, '_action_point_current', 0) or 0),
        )

    def _prepare_scheduling_action_point(self, fresh_ap, *, cost):
        """确定子任务后，在决策首读的同一个面板里完成开工补充并返回新读数。

        调用前须绑定子任务配置并设好它的行动力保留值，期间不能有地图操作。
        面板没有被保留时直接沿用传入读数，让子任务走原来的开工检查。

        Args:
            fresh_ap (tuple[int, int] | None): 本轮首读的 (总行动力, 当前行动力)。
            cost (int): 子任务开工检查所需的行动力。

        Returns:
            tuple[int, int] | None: 补充后的实际读数；没保留面板时原样返回。
        """
        if not getattr(self, '_scheduling_ap_panel_open', False):
            return fresh_ap
        self._scheduling_ap_panel_open = False
        if not self._is_in_action_point():
            return None

        # 只有「含箱口径没变」时才能把首读的读数直接拿来复用。
        same_box_use = self._scheduling_ap_box_use == self.config.OS_ACTION_POINT_BOX_USE
        if same_box_use and self.action_point_reusable(fresh_ap, cost):
            self.action_point_quit()
            return fresh_ap

        logger.info('[大世界-调度] 在首次读取的行动力面板内完成开工补充')
        if not self.handle_action_point(
            zone=None, pinned=None, cost=cost, keep_current_ap=True,
            check_rest_ap=True, skip_first_read=same_box_use,
        ):
            self.action_point_quit()
            return None
        # 统一在调度层把面板收尾：补充后的读数交给子任务，面板不再保留，
        # 否则它会挡住子任务接下来的地图操作（黄币 OCR、海域识别都会读到错值）。
        self.action_point_quit()
        return (int(self._action_point_total), int(self._action_point_current))

    # ------------------------------------------------------------ main entry

    def run_opsi_scheduler_once(self):
        """One scheduling round: read state, decide, run one sub-task round.

        Re-entered by `os_scheduling()` after every round, so resources, the
        month-end phase and every task's content are re-evaluated each time
        instead of running a fixed A -> B -> C sequence.
        """
        if self._delay_for_opsi_explore():
            return

        yellow_coins = self.get_yellow_coins()
        try:
            total_ap, current_ap = self._get_scheduling_action_point(keep_open=True)
            return self._run_opsi_scheduler_decision(yellow_coins, total_ap, current_ap)
        except (GameStuckError, GameTooManyClickError, RequestHumanTakeover):
            # 已经进入设备恢复流程，不再追加界面操作去干扰原异常
            self._scheduling_ap_panel_open = False
            raise
        finally:
            self._close_scheduling_action_point()

    def _run_opsi_scheduler_decision(self, yellow_coins, total_ap, current_ap):
        """使用首读的行动力做决策；面板在实际地图操作之前关闭或合并开工补充。"""
        meow_ap_preserve = self._get_scheduled_meow_ap_preserve()

        state = self._get_smart_state()
        coin_replenish_active = bool(state.get(STATE_KEY_COIN_REPLENISH, False))
        ap_replenish_active = bool(state.get(STATE_KEY_AP_REPLENISH, False))
        coin_target_mode = bool(self.config.OpsiScheduling_UseSmartSchedulingOperationCoinsPreserve)

        action, coin_replenish_active, ap_replenish_active = decide_resource_action(
            yellow_coins=yellow_coins,
            total_ap=total_ap,
            coin_preserve=self.config.OpsiScheduling_OperationCoinsPreserve,
            coin_return_threshold=self.config.OpsiScheduling_OperationCoinsReturnThreshold,
            ap_preserve=self.config.OpsiScheduling_ActionPointPreserve,
            meow_ap_preserve=meow_ap_preserve,
            coin_target_mode=coin_target_mode,
            coin_replenish_active=coin_replenish_active,
            ap_replenish_active=ap_replenish_active,
        )

        mode = self._decide_scheduling_mode(action, coin_target_mode)
        state_changed = self._sync_scheduling_mode(state, mode)
        if bool(state.get(STATE_KEY_COIN_REPLENISH, False)) != coin_replenish_active:
            state[STATE_KEY_COIN_REPLENISH] = coin_replenish_active
            state_changed = True
        if bool(state.get(STATE_KEY_AP_REPLENISH, False)) != ap_replenish_active:
            state[STATE_KEY_AP_REPLENISH] = ap_replenish_active
            state_changed = True
        if state_changed:
            self._save_smart_state(state)

        logger.info(f'[大世界-调度] 当前模式={mode}')
        logger.info(
            f'[大世界-调度] 黄币={yellow_coins}, AP={total_ap}, '
            f'黄币保留={self.config.OpsiScheduling_OperationCoinsPreserve}, '
            f'行动力保留={self.config.OpsiScheduling_ActionPointPreserve}, '
            f'补黄币保留={meow_ap_preserve}, 补黄币模式={coin_target_mode}, '
            f'coin_replenish_active={coin_replenish_active}, '
            f'ap_replenish_active={ap_replenish_active}')

        if action == 'wait':
            logger.info('[大世界-调度] 原因: 行动力达到保留值，无任务可执行')
            # 黄币没补够、行动力又跌破补黄币开工线，才叫「双重不足」；
            # 否则本轮只是撞在行动力保留线上（对齐 AP 的两条分支条件）。
            coin_preserve = int(self.config.OpsiScheduling_OperationCoinsPreserve)
            coins_short = coin_replenish_active or yellow_coins < coin_preserve
            if coins_short and total_ap <= meow_ap_preserve:
                notify_coins_ap_insufficient(
                    self, yellow_coins, total_ap,
                    self._get_coin_replenish_target(), meow_ap_preserve)
            else:
                notify_ap_insufficient(
                    self, total_ap, int(self.config.OpsiScheduling_ActionPointPreserve))
            self._delay_to_server_update('行动力不足')
            self.config.task_stop()

        if action == 'meow':
            result = self._dispatch_coin_task(yellow_coins, total_ap, meow_ap_preserve, current_ap)
            if result.status == OpsiStatus.NO_TASK:
                self._delay_to_server_update('未启用黄币补充任务')
                self.config.task_stop()
            if not result.executed:
                self._delay_to_server_update('黄币补充任务均无可执行内容')
                self.config.task_stop()
        else:
            if not self._is_hazard1_enabled():
                logger.warning(f'[大世界-调度] {TASK_NAME_HAZARD1_LEVELING} 未在智能调度中启用')
                self._delay_to_server_update('侵蚀1练级未启用')
                self.config.task_stop()
            result = self._execute_hazard1_leveling_once(yellow_coins, total_ap, current_ap)

        self._handle_opsi_task_result(result)

    def os_scheduling(self):
        """Entry point: run scheduling rounds until the task is stopped."""
        logger.info('[大世界-调度] 智能调度启动')
        while True:
            self.run_opsi_scheduler_once()
            self.config.check_task_switch()
