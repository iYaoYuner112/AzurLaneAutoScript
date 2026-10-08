from module.config.utils import get_os_reset_remain
from module.exception import RequestHumanTakeover, ScriptError
from module.logger import logger
from module.os_handler.action_point import ActionPointLimit
from module.map.map_grids import SelectedGrids
from module.os.map import ALREADY_SOLVED_MAP_EVENTS, OSMap
from module.os.opsi_notify import notify_action_point_change
from module.os.tasks.task_context import is_running_opsi_proxy


class OpsiMeowfficerFarming(OSMap):
    def _meow_fixed_patrol_scan(self):
        """
        Meowfficer's post-combat forced move (efficiency mode).

        Switch fleets to read each radar and clear question marks, moving none of
        them. This is equivalent to the CL1 fixed patrol L0/L1, and does not have
        the L2 fixed landing move (C1/D1/E1/F1 are defined for the hazard level 1
        map, not for the meowfficer zones).
        """
        if not self._meowfficer_patrol_enabled():
            return
        if self._solved_map_event & ALREADY_SOLVED_MAP_EVENTS:
            return
        logger.info('Meowfficer: trigger fixed patrol scan (switch fleets only)')
        # clear_question_any_fleet restores the primary fleet itself.
        self.clear_question_any_fleet()

    def os_meowfficer_farming(self, fresh_ap=None, ap_checked=False, ap_preserve=None):
        """耄耋相接入口。

        Args:
            fresh_ap (tuple[int, int] | None): 智能调度代跑时传入的决策首读
                (总行动力, 当前行动力)。决策暂留的面板还开着时，本轮的开工补充
                会在同一个面板里完成并更新这个读数；同海域续跑时再用它跳过
                进海域的行动点弹窗。
            ap_checked (bool): 智能调度代跑时为 True —— 本轮调度决策刚用新鲜
                读数验证过总行动力高于短猫保留线，短猫不必再开一次弹窗重复
                检查，否则一轮里会多出一组「REMAIN_OS + CANCEL」点击。
            ap_preserve (int | None): 智能调度代跑时本轮的补黄币开工线。
                None 表示独立运行，按任务自己的保留值配置。
        """
        logger.hr(f'OS meowfficer farming, hazard_level={self.config.OpsiMeowfficerFarming_HazardLevel}', level=1)
        if not ap_checked:
            # 独立运行：没有决策暂留的面板，也没有可复用的读数。
            self._close_scheduling_action_point()
            fresh_ap = None
        try:
            self._run_meowfficer_farming(
                fresh_ap=fresh_ap, ap_checked=ap_checked, ap_preserve=ap_preserve)
        finally:
            # 面板暂留期间任何出口都必须收尾（含 task_delay + task_stop 与异常），
            # 否则它会挡住后续的截图识别——黄币 OCR、海域名识别都会读到错值。
            self._close_scheduling_action_point()

    def _meow_preserve_value(self, ap_preserve=None) -> int:
        """本轮短猫的行动力保留线（总行动力低于或等于它就停手）。

        代跑时阈值由调度层给（对齐 AzurPilot 的 `ap_preserve` 传递），独立运行时
        仍按配置读。不再叠加月末动态保留、也不封顶：AzurPilot 没有官源的
        `get_action_point_limit()`，配置填多少就按多少停手。

        Args:
            ap_preserve (int | None): 智能调度本轮的补黄币开工线。

        Returns:
            int: 本轮生效的行动力保留值。
        """
        if ap_preserve is not None:
            return int(ap_preserve)
        if self.is_smart_scheduling_enabled:
            return int(self.config.OpsiScheduling_MeowfficerActionPointPreserve)
        return int(self.config.OpsiMeowfficerFarming_ActionPointPreserve)

    def _run_meowfficer_farming(self, fresh_ap=None, ap_checked=False, ap_preserve=None):
        """耄耋相接主流程：准备配置，然后按轮派发。行动力面板由调用方收尾。

        Args:
            fresh_ap (tuple[int, int] | None): 决策首读的 (总行动力, 当前行动力)。
            ap_checked (bool): 本轮是否已完成行动力检查。
            ap_preserve (int | None): 智能调度本轮的补黄币开工线。
        """
        # 500, not upstream's 1000, and keyed on "no line handed in by the scheduler"
        # rather than on the scheduling switch (AzurPilot's condition): a standalone
        # round still needs the CL1 profit floor.
        if ap_preserve is None and self.is_cl1_mode_enabled \
            and self.config.OpsiMeowfficerFarming_ActionPointPreserve < 500:
            logger.info('With CL1 leveling enabled, set action point preserve to 500')
            self.config.OpsiMeowfficerFarming_ActionPointPreserve = 500
        preserve = self._meow_preserve_value(ap_preserve=ap_preserve)
        if preserve == 0:
            self.config.override(OpsiFleet_Submarine=False)
        if self.is_cl1_mode_enabled:
            # Without these enabled, CL1 gains 0 profits
            self.config.override(
                OpsiGeneral_DoRandomMapEvent=True,
                OpsiGeneral_AkashiShopFilter='ActionPoint',
                OpsiFleet_Submarine=False,
            )
            cd = self.nearest_task_cooling_down
            logger.attr('Task cooling down', cd)
            # At the last day of every month, OpsiObscure and OpsiAbyssal are scheduled frequently
            # Don't schedule after them
            remain = get_os_reset_remain()
            if cd is not None and remain > 0:
                logger.info(f'Having task cooling down, delay OpsiMeowfficerFarming after it')
                self.config.task_delay(target=cd.next_run)
                self.config.task_stop()
        if self.is_in_opsi_explore():
            logger.warning(f'OpsiExplore is still running, cannot do {self.config.task.command}')
            self.config.task_delay(server_update=True)
            self.config.task_stop()

        while True:
            self.config.OS_ACTION_POINT_PRESERVE = preserve
            if self.config.is_task_enabled('OpsiAshBeacon') \
                    and not self._ash_fully_collected \
                    and self.config.cross_get("OpsiAshBeacon.OpsiAshBeacon.EnsureFullyCollected", True):
                logger.info('Ash beacon not fully collected, ignore action point limit temporarily')
                self.config.OS_ACTION_POINT_PRESERVE = 0
            logger.attr('OS_ACTION_POINT_PRESERVE', self.config.OS_ACTION_POINT_PRESERVE)
            if not ap_checked:
                # 独立运行时短猫自己查一次行动力下限；智能调度代跑时调度层已查过。
                ap_checked = self._meow_ap_check(ap_checked)

            # 决策暂留了行动力面板时，在这里把本轮开工补充并进同一个面板，
            # 并拿到可以复用的读数（对齐 AzurPilot 的 _prepare_scheduling_action_point）。
            fresh_ap = self._meow_prepare_action_point(fresh_ap)

            # ===== mode dispatch, same order as AzurPilot =====
            self._meow_dispatch(fresh_ap=fresh_ap)
            # 读数只能复用一次，下一轮必须重新读取。
            fresh_ap = None

            if self.is_smart_scheduling_enabled:
                if is_running_opsi_proxy(self.config):
                    return
                self.config.task_call('OpsiScheduling')
                self.config.task_stop()

    def _meow_ap_check(self, ap_checked):
        """
        行动力检查（对齐 AP master `_meow_ap_check`）。

        每轮先把 `OS_ACTION_POINT_PRESERVE` 设为本轮保留值（余烬信标未收满时置 0），
        首轮用 `action_point_set(cost=0, keep_current_ap=True)` 检查总行动力是否已
        跌破保留线。独立跑短猫（智能调度关闭）且黄币足够时，行动力不足会优雅推迟
        到服务器刷新，而不是以任务报错收场；被智能调度代理时异常交回调度层处理。

        智能调度代跑时这一步整个跳过（`ap_checked=True`）：调度决策刚用新鲜读数
        验证过总行动力高于保留线，再开一次弹窗只会多一组「REMAIN_OS + CANCEL」。

        Args:
            ap_checked (bool): 是否已完成本轮的行动力检查。

        Returns:
            bool: 最新的行动力检查状态标志。
        """
        if not ap_checked:
            keep_current_ap = True
            check_rest_ap = True
            smart_scheduled = is_running_opsi_proxy(self.config)
            cl1_yellow_enough = False
            if self.is_smart_scheduling_enabled:
                check_rest_ap = False
            if self.is_cl1_mode_enabled and not smart_scheduled:
                cl1_yellow_enough = self.get_yellow_coins() >= self.yellow_coins_preserve
                if cl1_yellow_enough:
                    check_rest_ap = False
            if not self.is_cl1_mode_enabled and self.config.OpsiGeneral_BuyActionPointLimit > 0:
                keep_current_ap = False
            if not smart_scheduled and self.is_cl1_mode_enabled and cl1_yellow_enough:
                try:
                    self.action_point_set(
                        cost=0, keep_current_ap=keep_current_ap, check_rest_ap=check_rest_ap)
                except ActionPointLimit as e:
                    logger.warning(
                        f'[短猫相接] 行动力达到保留线 '
                        f'(total={e.total} <= preserve={e.preserve})，推迟到服务器刷新')
                    self.config.task_delay(server_update=True)
                    self.config.task_stop()
            else:
                self.action_point_set(
                    cost=0, keep_current_ap=keep_current_ap, check_rest_ap=check_rest_ap)
            # 独立跑短猫时由本任务报行动力变化；被智能调度代理时决策首读已经报过。
            if not smart_scheduled:
                notify_action_point_change(self)
            ap_checked = True
        return ap_checked

    def _meow_prepare_action_point(self, fresh_ap):
        """把智能调度决策首读的行动力面板用于本轮开工。

        已在目标安全海域续跑时，直接在决策暂留的同一个面板里完成开工补充
        （对齐 AzurPilot）：开工线 120 的弹窗逻辑与「复用读数」结果一致，
        可以省掉一次「关窗 → 重进海域 → 重开弹窗」。

        换海域、还没进海域或传统单一海域模式则先关窗，由各自的进海域流程
        重新补充——那时读数会作废，强行复用会漏掉开箱与石油购买。

        Args:
            fresh_ap (tuple[int, int] | None): 决策首读的 (总行动力, 当前行动力)。

        Returns:
            tuple[int, int] | None: 开工补充后的读数；没能保留面板或已关窗时返回 None。
        """
        if not getattr(self, '_scheduling_ap_panel_open', False):
            # 面板没被保留：读数没有对应的新鲜来源，不能复用。
            return None
        stay_in_zone = self.config.OpsiMeowfficerFarming_StayInZone
        target_zone = self.config.OpsiMeowfficerFarming_TargetZone
        if stay_in_zone and target_zone != 0 \
                and getattr(getattr(self, 'zone', None), 'zone_id', None) == target_zone \
                and self.is_zone_name_hidden:
            # 已在单个指定安全海域：首读面板可直接完成本轮开工补充。
            return self._prepare_scheduling_action_point(fresh_ap, cost=120)
        # 需要换海域或走传统模式：先关窗，沿各自的进入海域流程补充。
        self._close_scheduling_action_point()
        return None

    def _meow_dispatch(self, fresh_ap=None):
        """
        Pick and run the meowfficer mode handler, in AzurPilot's order:

        1. traditional single target zone (TargetZone given, StayInZone off);
        2. StayInZone: keep searching the configured target zone;
        3. random zone search.

        Args:
            fresh_ap (tuple[int, int] | None): 本轮开工可复用的行动力首读，
                只在「指定海域 + StayInZone」模式下消费。
        """
        target_zone = self.config.OpsiMeowfficerFarming_TargetZone
        stay_in_zone = self.config.OpsiMeowfficerFarming_StayInZone
        if target_zone != 0:
            try:
                zone = self.name_to_zone(target_zone)
            except ScriptError:
                logger.warning(f'wrong zone_id input:{target_zone}')
                raise RequestHumanTakeover('wrong input, task stopped')
            if stay_in_zone:
                self._meow_handle_stay_in_zone(zone, fresh_ap=fresh_ap)
            else:
                self._meow_handle_traditional_zone(zone)
        else:
            if stay_in_zone:
                logger.warning('StayInZone is enabled but TargetZone is 0, '
                               'fallback to the random zone search')
            self._meow_handle_normal_search()

    def _meow_handle_traditional_zone(self, zone):
        """
        Traditional single target zone (AzurPilot's `_meow_handle_traditional_zone`).

        One strategic search round; the whole map gets rescanned inside
        `run_strategic_search`, then the radar-only patrol runs. When the search was
        interrupted the map state is not trustworthy, so the radar patrol is skipped
        and the next round picks it up (AzurPilot's `if search_completed:` gate).
        """
        logger.hr(f'OS meowfficer farming, zone_id={zone.zone_id}', level=1)
        self.globe_goto(zone, refresh=True)
        self.fleet_set(self.config.OpsiFleet_Fleet)
        self.os_order_execute(
            recon_scan=False,
            submarine_call=self.config.OpsiFleet_Submarine)
        if self.run_strategic_search():
            self._meow_fixed_patrol_scan()
        else:
            logger.warning('Strategic search interrupted, skip the radar patrol this round')
        self.handle_after_auto_search()
        self.config.check_task_switch()

    def _meow_handle_stay_in_zone(self, zone, fresh_ap=None):
        """
        Stay in one target zone and keep searching (AzurPilot's `_meow_handle_stay_in_zone`).

        Get into the zone, prepare 120 action points, then run one strategic search
        round with the radar-only patrol, and repeat next round.

        Args:
            zone (Zone): 目标海域。
            fresh_ap (tuple[int, int] | None): 智能调度决策首读、或上个面板里
                补充完成的 (总行动力, 当前行动力)。与本次调用之间没有行动力消耗，
                达到 120 开工线时直接复用，跳过行动点弹窗。
        """
        logger.hr(f'OS meowfficer farming (stay in zone), zone_id={zone.zone_id}', level=1)
        self.get_current_zone()
        if self.zone.zone_id != zone.zone_id or not self.is_zone_name_hidden:
            self.globe_goto(zone, types='SAFE', refresh=True)
            # 换海域会消耗行动力，开工检查必须重新读取。
            fresh_ap = None
        if self.action_point_reusable(fresh_ap, cost=120):
            fresh_total, fresh_current = fresh_ap
            logger.info(
                f'[大世界-耄耋相接] 复用刚读到的行动力'
                f'(当前={fresh_current}, 总={fresh_total})，跳过行动点弹窗')
        else:
            self.action_point_set(cost=120, keep_current_ap=True, check_rest_ap=True)
        self.fleet_set(self.config.OpsiFleet_Fleet)
        self.os_order_execute(
            recon_scan=False,
            submarine_call=self.config.OpsiFleet_Submarine)
        if self.run_strategic_search():
            self._meow_fixed_patrol_scan()
        else:
            logger.warning('Strategic search interrupted, skip the radar patrol this round')
        self.handle_after_auto_search()
        self.config.check_task_switch()

    def _meow_handle_normal_search(self):
        """
        Random zone search: pick the nearest usable zone of the configured hazard
        level (AzurPilot's `_meow_handle_normal_search`).
        """
        # (1252, 1012) is the coordinate of zone 134 (the center zone) in os_globe_map.png
        zones = self.zone_select(hazard_level=self.config.OpsiMeowfficerFarming_HazardLevel) \
            .delete(SelectedGrids([self.zone])) \
            .delete(SelectedGrids(self.zones.select(is_port=True))) \
            .sort_by_clock_degree(center=(1252, 1012), start=self.zone.location)

        if not zones:
            # Every zone of the configured hazard level is cleared. Without this guard the
            # next line raises IndexError and the task dies (AzurPilot 30b5a2b7d).
            hazard = self.config.OpsiMeowfficerFarming_HazardLevel
            logger.warning(f'No zone left for hazard level {hazard}, nothing to search')
            self.config.task_delay(server_update=True)
            self.config.task_stop()
            return False

        logger.hr(f'OS meowfficer farming, zone_id={zones[0].zone_id}', level=1)
        self.globe_goto(zones[0])
        self.fleet_set(self.config.OpsiFleet_Fleet)
        self.os_order_execute(
            recon_scan=False,
            submarine_call=self.config.OpsiFleet_Submarine)
        self.run_auto_search()
        self._meow_fixed_patrol_scan()
        self.handle_after_auto_search()
        self.config.check_task_switch()
