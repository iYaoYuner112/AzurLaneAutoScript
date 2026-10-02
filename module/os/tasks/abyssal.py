from module.config.utils import get_os_reset_remain
from module.exception import RequestHumanTakeover
from module.logger import logger
from module.os.map import OSMap
from module.os.tasks.task_context import opsi_no_content, should_hand_over_to_scheduling


class OpsiAbyssal(OSMap):
    def delay_abyssal(self, result=True):
        """
        Args:
            result(bool): If still have obscure coordinates.
        """
        if get_os_reset_remain() == 0:
            logger.info('Just less than 1 day to OpSi reset, delay 2.5 hours')
            # A delay, not a "no content": the round ends and the delay is
            # attributed to the scheduler through the task context owner.
            self.config.task_delay(minute=150, server_update=True)
            self.config.task_stop()
        elif not result:
            # No abyssal loggers: proxied runs report NO_CONTENT so the
            # scheduler can try the next candidate task.
            opsi_no_content(self.config, 'OpsiAbyssal', 'No abyssal loggers')

    def clear_abyssal(self):
        """
        Get one abyssal logger in storage,
        attack abyssal boss,
        repair fleets in port.

        Raises:
            ActionPointLimit:
            TaskEnd: If no more abyssal loggers.
            RequestHumanTakeover: If unable to clear boss, fleets exhausted.
        """
        logger.hr('OS clear abyssal', level=1)
        self.cl1_ap_preserve()

        with self.config.temporary(STORY_ALLOW_SKIP=False):
            result = self.storage_get_next_item('ABYSSAL', use_logger=self.config.OpsiGeneral_UseLogger)
        if not result:
            self.delay_abyssal(result=False)

        self.config.override(
            OpsiGeneral_DoRandomMapEvent=False,
            HOMO_EDGE_DETECT=False,
            STORY_OPTION=0
        )
        self.zone_init()
        result = self.run_abyssal()
        if not result:
            raise RequestHumanTakeover

        self.fleet_repair(revert=False)
        self.delay_abyssal()

    def os_abyssal(self):
        if should_hand_over_to_scheduling(
                self.config, 'OpsiAbyssal', self.is_smart_scheduling_enabled):
            logger.info('[大世界-调度] OpsiAbyssal 交由智能调度统一代理，交接给 OpsiScheduling')
            self.config.task_call('OpsiScheduling')
            self.config.task_stop()

        while True:
            self.clear_abyssal()
            self.config.check_task_switch()
