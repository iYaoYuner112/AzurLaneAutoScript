from module.config.utils import get_os_reset_remain
from module.exception import RequestHumanTakeover, ScriptError
from module.logger import logger
from module.os_handler.action_point import ActionPointLimit
from module.map.map_grids import SelectedGrids
from module.os.map import ALREADY_SOLVED_MAP_EVENTS, OSMap
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
        enabled = getattr(self.config, 'OpsiMeowfficerFarming_ExecuteFixedPatrolScan', None)
        if enabled is None:
            enabled = self.config.cross_get(
                'OpsiMeowfficerFarming.OpsiMeowfficerFarming.ExecuteFixedPatrolScan', default=False)
        if not enabled:
            return
        if self._solved_map_event & ALREADY_SOLVED_MAP_EVENTS:
            return
        logger.info('Meowfficer: trigger fixed patrol scan (switch fleets only)')
        # clear_question_any_fleet restores the primary fleet itself.
        self.clear_question_any_fleet()

    def os_meowfficer_farming(self, fresh_ap=None):
        """耄耋相接入口。

        Args:
            fresh_ap (tuple[int, int] | None): 智能调度代跑时传入的决策首读。
                短猫的行动力前置检查（`_meow_ap_check`）自带完整的弹窗流程，
                这一步会把决策暂留的面板消耗掉，所以该读数在这里不复用——
                只负责把面板收尾，避免它残留到后续的截图识别。
        """
        logger.hr(f'OS meowfficer farming, hazard_level={self.config.OpsiMeowfficerFarming_HazardLevel}', level=1)
        # 收尾决策暂留的行动力面板（短猫的开工检查会自己重开弹窗）
        self._close_scheduling_action_point()
        if self.is_cl1_mode_enabled and not self.is_smart_scheduling_enabled \
            and self.config.OpsiMeowfficerFarming_ActionPointPreserve < 1000:
            logger.info('With CL1 leveling enabled, set action point preserve to 1000')
            self.config.OpsiMeowfficerFarming_ActionPointPreserve = 1000
        action_point_preserve = (
            self.config.OpsiScheduling_MeowfficerActionPointPreserve
            if self.is_smart_scheduling_enabled
            else self.config.OpsiMeowfficerFarming_ActionPointPreserve
        )
        preserve = min(self.get_action_point_limit(), action_point_preserve, 2000)
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

        ap_checked = False
        while True:
            self.config.OS_ACTION_POINT_PRESERVE = preserve
            if self.config.is_task_enabled('OpsiAshBeacon') \
                    and not self._ash_fully_collected \
                    and self.config.cross_get("OpsiAshBeacon.OpsiAshBeacon.EnsureFullyCollected", True):
                logger.info('Ash beacon not fully collected, ignore action point limit temporarily')
                self.config.OS_ACTION_POINT_PRESERVE = 0
            logger.attr('OS_ACTION_POINT_PRESERVE', self.config.OS_ACTION_POINT_PRESERVE)
            if not ap_checked:
                ap_checked = self._meow_ap_check(ap_checked)

            # ===== mode dispatch, same order as AzurPilot =====
            self._meow_dispatch()

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
            ap_checked = True
        return ap_checked

    def _meow_dispatch(self):
        """
        Pick and run the meowfficer mode handler, in AzurPilot's order:

        1. traditional single target zone (TargetZone given, StayInZone off);
        2. StayInZone: keep searching the configured target zone;
        3. random zone search.
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
                self._meow_handle_stay_in_zone(zone)
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
        `run_strategic_search`, then the radar-only patrol runs.
        """
        logger.hr(f'OS meowfficer farming, zone_id={zone.zone_id}', level=1)
        self.globe_goto(zone, refresh=True)
        self.fleet_set(self.config.OpsiFleet_Fleet)
        self.os_order_execute(
            recon_scan=False,
            submarine_call=self.config.OpsiFleet_Submarine)
        self.run_strategic_search()
        self._meow_fixed_patrol_scan()
        self.handle_after_auto_search()
        self.config.check_task_switch()

    def _meow_handle_stay_in_zone(self, zone):
        """
        Stay in one target zone and keep searching (AzurPilot's `_meow_handle_stay_in_zone`).

        Get into the zone, prepare 120 action points, then run one strategic search
        round with the radar-only patrol, and repeat next round.
        """
        logger.hr(f'OS meowfficer farming (stay in zone), zone_id={zone.zone_id}', level=1)
        self.get_current_zone()
        if self.zone.zone_id != zone.zone_id or not self.is_zone_name_hidden:
            self.globe_goto(zone, types='SAFE', refresh=True)
        self.action_point_set(cost=120, keep_current_ap=True, check_rest_ap=True)
        self.fleet_set(self.config.OpsiFleet_Fleet)
        self.os_order_execute(
            recon_scan=False,
            submarine_call=self.config.OpsiFleet_Submarine)
        self.run_strategic_search()
        self._meow_fixed_patrol_scan()
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
