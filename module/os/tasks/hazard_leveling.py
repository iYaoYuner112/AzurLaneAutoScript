from module.logger import logger
from module.os.map import OSMap
from module.os.tasks.task_context import is_running_opsi_proxy


class OpsiHazard1Leveling(OSMap):
    def os_hazard1_leveling(self, fresh_ap=None):
        """侵蚀 1 练级入口。

        Args:
            fresh_ap (tuple[int, int] | None): 智能调度代理执行时传入的
                (总行动力, 当前行动力) 首读；用于在同一个行动力面板里确认开工、
                跳过重复的弹窗往返。独立运行时为 None。
        """
        logger.hr('OS hazard 1 leveling', level=1)
        # Without these enabled, CL1 gains 0 profits
        self.config.override(
            OpsiGeneral_DoRandomMapEvent=True,
            OpsiGeneral_AkashiShopFilter='ActionPoint',
        )
        if not self.is_smart_scheduling_enabled and not self.config.is_task_enabled('OpsiMeowfficerFarming'):
            self.config.cross_set(keys='OpsiMeowfficerFarming.Scheduler.Enable', value=True)
        while True:
            # Limited action point preserve of hazard 1 to 200
            self.config.OS_ACTION_POINT_PRESERVE = (
                self.config.OpsiScheduling_ActionPointPreserve
                if self.is_smart_scheduling_enabled else 200
            )
            if self.config.is_task_enabled('OpsiAshBeacon') \
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
