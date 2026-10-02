from module.logger import logger
from module.os.map import OSMap


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
    """Choose the next Operation Siren task from current coin and AP totals.

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


class OpsiScheduling(OSMap):
    def _run_with_child_config(self, task, func):
        self.config.bind('OpsiScheduling', func_list=[task])
        try:
            func()
        finally:
            self.config.bind('OpsiScheduling')

    def os_scheduling(self):
        if self.is_in_opsi_explore():
            logger.info('OpsiExplore is still running, delay resource scheduling')
            self.config.task_delay(server_update=True)
            self.config.task_stop()

        while True:
            yellow_coins = self.get_yellow_coins()
            self.action_point_enter()
            self.action_point_safe_get()
            total_ap = int(self._action_point_total)
            self.action_point_quit()
            meow_ap_preserve = min(
                self.get_action_point_limit(),
                self.config.OpsiScheduling_MeowfficerActionPointPreserve,
                2000,
            )

            state = self.config.cross_get('OpsiScheduling.Storage.Storage', default={})
            if not isinstance(state, dict):
                state = {}
            coin_replenish_active = bool(state.get('CoinReplenishActive', False))
            ap_replenish_active = bool(state.get('ApReplenishActive', False))
            action, coin_replenish_active, ap_replenish_active = decide_resource_action(
                yellow_coins=yellow_coins,
                total_ap=total_ap,
                coin_preserve=self.config.OpsiScheduling_OperationCoinsPreserve,
                coin_return_threshold=self.config.OpsiScheduling_OperationCoinsReturnThreshold,
                ap_preserve=self.config.OpsiScheduling_ActionPointPreserve,
                meow_ap_preserve=meow_ap_preserve,
                coin_target_mode=self.config.OpsiScheduling_UseSmartSchedulingOperationCoinsPreserve,
                coin_replenish_active=coin_replenish_active,
                ap_replenish_active=ap_replenish_active,
            )

            state_changed = False
            if state.get('CoinReplenishActive', False) != coin_replenish_active:
                state['CoinReplenishActive'] = coin_replenish_active
                state_changed = True
            if state.get('ApReplenishActive', False) != ap_replenish_active:
                state['ApReplenishActive'] = ap_replenish_active
                state_changed = True
            if state_changed:
                self.config.cross_set('OpsiScheduling.Storage.Storage', state)

            logger.info(
                f'OpSi resource scheduling: coins={yellow_coins}, total_ap={total_ap}, '
                f'action={action}, coin_replenish_active={coin_replenish_active}, '
                f'ap_replenish_active={ap_replenish_active}'
            )
            if action == 'wait':
                self.config.task_delay(server_update=True)
                self.config.task_stop()

            if action == 'meow':
                task = 'OpsiMeowfficerFarming'
                enabled = self.config.OpsiScheduling_EnableMeowfficerFarming
            else:
                task = 'OpsiHazard1Leveling'
                enabled = self.config.OpsiScheduling_EnableHazard1Leveling
            if not enabled:
                logger.warning(f'{task} is disabled in OpsiScheduling')
                self.config.task_delay(server_update=True)
                self.config.task_stop()

            if action == 'meow':
                self._run_with_child_config(
                    'OpsiMeowfficerFarming', self.os_meowfficer_farming
                )
            else:
                self._run_with_child_config(
                    'OpsiHazard1Leveling', self.os_hazard1_leveling
                )
            self.config.check_task_switch()