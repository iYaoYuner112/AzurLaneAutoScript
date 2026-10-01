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
):
    """Choose the next Operation Siren task from current coin and AP totals."""
    coin_preserve = max(int(coin_preserve), 0)
    coin_return_threshold = max(int(coin_return_threshold), 0)
    ap_preserve = max(int(ap_preserve), 0)
    meow_ap_preserve = max(int(meow_ap_preserve), 0)

    if total_ap <= ap_preserve:
        return 'wait', coin_replenish_active

    if coin_target_mode:
        coin_replenish_active = coin_replenish_active or yellow_coins < coin_preserve
        coin_target = coin_preserve + coin_return_threshold
        if coin_replenish_active and yellow_coins < coin_target:
            if total_ap <= max(ap_preserve, meow_ap_preserve):
                return 'wait', True
            return 'meow', True
        return 'cl1', False

    if yellow_coins < coin_preserve:
        if total_ap <= max(ap_preserve, meow_ap_preserve):
            return 'wait', False
        return 'meow', False
    return 'cl1', False


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

            state = self.config.cross_get('OpsiScheduling.Storage.Storage', default={})
            if not isinstance(state, dict):
                state = {}
            coin_replenish_active = bool(state.get('CoinReplenishActive', False))
            action, coin_replenish_active = decide_resource_action(
                yellow_coins=yellow_coins,
                total_ap=total_ap,
                coin_preserve=self.config.OpsiScheduling_OperationCoinsPreserve,
                coin_return_threshold=self.config.OpsiScheduling_OperationCoinsReturnThreshold,
                ap_preserve=self.config.OpsiScheduling_ActionPointPreserve,
                meow_ap_preserve=self.config.OpsiScheduling_MeowfficerActionPointPreserve,
                coin_target_mode=self.config.OpsiScheduling_UseSmartSchedulingOperationCoinsPreserve,
                coin_replenish_active=coin_replenish_active,
            )

            if state.get('CoinReplenishActive', False) != coin_replenish_active:
                state['CoinReplenishActive'] = coin_replenish_active
                self.config.cross_set('OpsiScheduling.Storage.Storage', state)

            logger.info(
                f'OpSi resource scheduling: coins={yellow_coins}, total_ap={total_ap}, '
                f'action={action}, coin_replenish_active={coin_replenish_active}'
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