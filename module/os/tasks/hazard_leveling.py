from module.logger import logger
from module.os.map import OSMap
from module.os.opsi_notify import notify_action_point_change
from module.os.tasks.task_context import is_running_opsi_proxy, is_running_prevent_overflow


class OpsiHazard1Leveling(OSMap):
    def os_hazard1_leveling(self, fresh_ap=None, ap_preserve=None):
        """侵蚀 1 练级入口。

        Args:
            fresh_ap (tuple[int, int] | None): 智能调度代理执行时传入的
                (总行动力, 当前行动力) 首读；用于在同一个行动力面板里确认开工、
                跳过重复的弹窗往返。独立运行时为 None。
            ap_preserve (int | None): 调用方指定的行动力保留值，优先级最高。
                防止行动力溢出传下限（要花光）；智能调度代跑传 None 走配置。
        """
        logger.hr('OS hazard 1 leveling', level=1)
        # Without these enabled, CL1 gains 0 profits
        self.config.override(
            OpsiGeneral_DoRandomMapEvent=True,
            OpsiGeneral_AkashiShopFilter='ActionPoint',
        )
        if not self.is_smart_scheduling_enabled and not self.config.is_task_enabled('OpsiMeowfficerFarming'):
            self.config.cross_set(keys='OpsiMeowfficerFarming.Scheduler.Enable', value=True)
        if ap_preserve is not None:
            logger.attr('侵蚀1行动力保留', int(ap_preserve))
        while True:
            # 保留值优先级：调用方指定 > 智能调度配置 > 独立运行固定 200
            if ap_preserve is not None:
                self.config.OS_ACTION_POINT_PRESERVE = int(ap_preserve)
            else:
                self.config.OS_ACTION_POINT_PRESERVE = (
                    self.config.OpsiScheduling_ActionPointPreserve
                    if self.is_smart_scheduling_enabled else 200
                )
            # 塞壬信标没收完时忽略保留值，是为了「先收信标」；但防止行动力溢出
            # 代跑时清零会把用户设的下限一并作废（实测会把行动力花光），
            # 所以这种放行只在正常代跑/独立运行时生效。
            if not is_running_prevent_overflow(self.config) \
                    and self.config.is_task_enabled('OpsiAshBeacon') \
                    and not self._ash_fully_collected \
                    and self.config.cross_get("OpsiAshBeacon.OpsiAshBeacon.EnsureFullyCollected", True):
                logger.info('Ash beacon not fully collected, ignore action point limit temporarily')
                self.config.OS_ACTION_POINT_PRESERVE = 0
            logger.attr('OS_ACTION_POINT_PRESERVE', self.config.OS_ACTION_POINT_PRESERVE)

            # 智能调度代跑时，决策会暂留行动力面板：必须在任何地图操作（读黄币、读海域）
            # 之前处理掉。否则面板挡住地图，黄币 OCR 读到 0，会被误判成「黄币达到上限」
            # 而直接空转；空转又会加速调度循环，一轮一次开窗关窗，攒够 12 次交替点击
            # 就触发防连点、重启游戏。
            if is_running_opsi_proxy(self.config):
                fresh_ap = self._prepare_scheduling_action_point(fresh_ap, cost=70)

            # 决策代跑时刚读过黄币、也已经按保留线分派过任务，这里不再进情报页
            # 重读一遍（对齐 AzurPilot：智能调度上下文跳过侵蚀1 的开工黄币检查）。
            if not is_running_opsi_proxy(self.config):
                self._cl1_resource_check()

            self.get_current_zone()

            # Preset action point to 70
            # When running CL1 oil is for running CL1, not meowfficer farming
            keep_current_ap = True
            if self.config.OpsiGeneral_BuyActionPointLimit > 0:
                keep_current_ap = False
            if keep_current_ap and self.action_point_reusable(fresh_ap, cost=70):
                _fresh_total, _fresh_current = fresh_ap
                logger.info(
                    f'[大世界-侵蚀1练级] 复用刚读到的行动力'
                    f'(当前={_fresh_current}, 总={_fresh_total})，跳过行动点弹窗')
            else:
                self.action_point_set(cost=70, keep_current_ap=keep_current_ap, check_rest_ap=True)
            # 首读只复用一次：面板已经关掉了，后续轮次照常走弹窗
            fresh_ap = None
            # 独立跑侵蚀 1 时由本任务报行动力变化；被智能调度代理时，决策首读已经报过一次。
            if not is_running_opsi_proxy(self.config):
                notify_action_point_change(self)
            if self._action_point_total >= 3000 and not self.is_smart_scheduling_enabled:
                with self.config.multi_set():
                    self.config.task_delay(server_update=True)
                    if not self.is_in_opsi_explore():
                        cd = self.nearest_task_cooling_down
                        if cd is None:
                            for task in ['OpsiAbyssal', 'OpsiStronghold', 'OpsiObscure']:
                                if self.config.is_task_enabled(task):
                                    self.config.task_call(task)
                        self.config.task_call('OpsiMeowfficerFarming')
                self.config.task_stop()

            if self.config.OpsiHazard1Leveling_TargetZone != 0:
                zone = self.config.OpsiHazard1Leveling_TargetZone
            else:
                zone = 22
            logger.hr(f'OS hazard 1 leveling, zone_id={zone}', level=1)
            if self.zone.zone_id != zone or not self.is_zone_name_hidden:
                self.globe_goto(self.name_to_zone(zone), types='SAFE', refresh=True)
            self.fleet_set(self.config.OpsiFleet_Fleet)
            # 续查上次看到却没处理完的地图事件（点击/自律途中卡死重启后）：
            # 明石、塞壬装置在这里先补完，避免重开一局把这只猫/这个装置漏掉。
            # 全图扫描与强制移动都没能看到事件时，续查会自行清记录继续下一轮。
            self._resume_pending_map_event()
            if not self.run_strategic_search():
                logger.warning('Strategic search was interrupted, scan the map anyway')
            # Fixed patrol: read the radars of all fleets without moving any of
            # them first, then move fleets away and rescan the whole map when
            # needed. Skipped when an event is already solved (AzurPilot puts the
            # check here, not inside the patrol entry).
            if self._forced_move_enabled():
                if not self._solved_map_event:
                    self.execute_fixed_patrol_scan()

            self.handle_after_auto_search()
            self.config.check_task_switch()
            if self.is_smart_scheduling_enabled:
                if is_running_opsi_proxy(self.config):
                    return
                self.config.task_call('OpsiScheduling')
                self.config.task_stop()

    def _cl1_resource_check(self):
        """
        开工黄币检查，只给非代理运行（独立侵蚀1、或被 task_call 直接拉起）用。

        智能调度代跑时不再调用：决策每轮开头已经读过黄币并按保留线分派过任务，
        再读一次只是多一趟情报页往返。

        Raises:
            ScriptEnd: 黄币不足时交棒给智能调度或其它任务。
        """
        coin_preserve = (
            self.config.OpsiScheduling_OperationCoinsPreserve
            if self.is_smart_scheduling_enabled
            else self.yellow_coins_preserve
        )
        if self.get_yellow_coins() >= coin_preserve:
            return

        logger.info(f'Reach the limit of yellow coins, preserve={coin_preserve}')
        if self.is_smart_scheduling_enabled:
            self.config.task_call('OpsiScheduling')
            self.config.task_stop()
        with self.config.multi_set():
            self.config.task_delay(server_update=True)
            if not self.is_in_opsi_explore():
                cd = self.nearest_task_cooling_down
                if cd is None:
                    for task in ['OpsiAbyssal', 'OpsiStronghold', 'OpsiObscure']:
                        if self.config.is_task_enabled(task):
                            self.config.task_call(task)
                self.config.task_call('OpsiMeowfficerFarming')
        self.config.task_stop()

    def run_hazard1_leveling_once(self, ap_preserve=None, fresh_ap=None):
        """执行一轮侵蚀 1 练级（智能调度 / 防止行动力溢出共用入口）。

        对齐 AzurPilot 的同名方法：把本轮的保留值显式传进来，而不是让子任务
        自己去读配置——防溢出要的是「花到下限」，不能沿用 200 的保留线。

        Args:
            ap_preserve (int | None): 本轮行动力保留值；None 时沿用配置。
            fresh_ap (tuple[int, int] | None): 调用方刚读到的
                (总行动力, 当前行动力)，用于省掉一次面板往返。

        Returns:
            OpsiTaskResult: 本轮子任务的执行结果。
        """
        yellow_coins = self.get_yellow_coins_for_decision()
        if fresh_ap is None:
            fresh_ap = self._get_scheduling_action_point(keep_open=True)
        try:
            # 保留值走参数直传（与 AzurPilot 一致），不再临时改配置
            return self._execute_hazard1_leveling_once(
                yellow_coins, fresh_ap[0], fresh_ap[1], ap_preserve=ap_preserve)
        finally:
            self._close_scheduling_action_point()
