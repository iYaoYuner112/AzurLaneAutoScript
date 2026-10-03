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

from module.config.utils import get_os_reset_remain, get_server_next_update
from module.logger import logger
from module.os.map import OSMap
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

    # ------------------------------------------------------------- postpone

    def _postpone_coin_task_check(self, task_name, reason=''):
        """Remember that this candidate was cleared, so it is skipped for a while."""
        state_key = COIN_TASK_POSTPONE_KEYS.get(task_name)
        if state_key is None:
            return
        if get_os_reset_remain() <= 0:
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
        try:
            with opsi_task_context(
                self.config,
                task_name,
                disable_task_switch=COIN_TASK_DISABLE_TASK_SWITCH.get(task_name, True),
            ):
                handler()
        except OpsiNoContent as e:
            self._postpone_coin_task_check(task_name, str(e))
            return OpsiTaskResult(OpsiStatus.NO_CONTENT, task=task_name, reason=str(e))

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
        # 侵蚀 1 练级自带完整的清场流程（计划作战 + clear_question + map_rescan），
        # 所以 os_init 挂起的首次自律寻敌在这里必须跳过：一旦先跑了自律，
        # 当前海域会被打空，随后的计划作战无目标可打，地图事件也就失去了
        # 被 map_rescan 处理的机会（这正是「自律完直接进计划作战、事件被漏」的原因）。
        # AzurPilot 同样在代理侵蚀 1 前 handle_first_auto_search(run=False)。
        self.handle_first_auto_search(run=False)
        try:
            with opsi_task_context(self.config, TASK_NAME_HAZARD1_LEVELING, disable_task_switch=False):
                self.os_hazard1_leveling()
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
        self.action_point_enter()
        self.action_point_safe_get()
        total_ap = int(self._action_point_total)
        self.action_point_quit()
        current_ap = total_ap
        meow_ap_preserve = min(
            self.get_action_point_limit(),
            self.config.OpsiScheduling_MeowfficerActionPointPreserve,
            2000,
        )

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
