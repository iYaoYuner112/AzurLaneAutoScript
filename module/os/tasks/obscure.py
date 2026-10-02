from module.config.utils import get_os_reset_remain
from module.logger import logger
from module.os.map import OSMap
from module.os.tasks.task_context import opsi_no_content, should_hand_over_to_scheduling


class OpsiObscure(OSMap):
    def clear_obscure(self):
        """
        Raises:
            ActionPointLimit:
            OpsiNoContent: If proxied by the scheduler and there is no coordinate.
        """
        logger.hr('OS clear obscure', level=1)
        self.cl1_ap_preserve()
        if self.config.OpsiObscure_ForceRun:
            logger.info('OS obscure finish is under force run')

        result = self.storage_get_next_item('OBSCURE', use_logger=self.config.OpsiGeneral_UseLogger)
        if not result:
            # No obscure coordinates. Proxied runs report NO_CONTENT so the
            # scheduler tries the next candidate; standalone runs keep the
            # legacy "delay until tomorrow" behaviour.
            opsi_no_content(
                self.config, 'OpsiObscure', 'No obscure coordinates',
                minute=150 if get_os_reset_remain() == 0 else None)

        self.config.override(
            OpsiGeneral_DoRandomMapEvent=False,
            HOMO_EDGE_DETECT=False,
            STORY_OPTION=0,
        )
        self.zone_init()
        self.fleet_set(self.config.OpsiFleet_Fleet)
        self.os_order_execute(
            recon_scan=True,
            submarine_call=self.config.OpsiFleet_Submarine)
        self.run_auto_search(rescan='current')

        self.map_exit()
        self.handle_after_auto_search()

    def os_obscure(self):
        if should_hand_over_to_scheduling(
                self.config, 'OpsiObscure', self.is_smart_scheduling_enabled):
            logger.info('[大世界-调度] OpsiObscure 交由智能调度统一代理，交接给 OpsiScheduling')
            self.config.task_call('OpsiScheduling')
            self.config.task_stop()

        while True:
            self.clear_obscure()
            if self.config.OpsiObscure_ForceRun:
                self.config.check_task_switch()
                continue
            else:
                break
