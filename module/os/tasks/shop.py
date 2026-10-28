from datetime import datetime, timedelta

from module.config.utils import get_os_next_reset, get_server_next_update, get_os_reset_remain
from module.logger import logger
from module.os.map import OSMap
from module.os_handler.assets import EXCHANGE_CHECK, EXCHANGE_ENTER
from module.os_shop.assets import OS_SHOP_CHECK


class OpsiShop(OSMap):
    def os_shop(self):
        """
        Buy all supplies in all ports.
        If not having enough yellow coins or purple coins, skip buying supplies in next port.
        """
        logger.hr('OS port daily', level=1)
        # 月初前 N 天不跑商店：月初商店可能没刷新，或想把黄币留给别的任务。
        # 默认 0 = 不限制，行为与改动之前完全一致。
        disable_before = int(self.config.OpsiShop_DisableBeforeDate or 0)
        today = datetime.now().day
        if disable_before > 0 and today <= disable_before:
            logger.info(f'[大世界-商店] 今日 {today} 号 <= 限制日期 {disable_before}，'
                        '按配置跳过本日商店')
            self.config.task_delay(server_update=True)
            self.config.task_stop()

        if not self.zone.is_azur_port:
            self.globe_goto(self.zone_nearest_azur_port(self.zone))

        self.port_enter()
        self.port_shop_enter()

        if self.appear(OS_SHOP_CHECK):
            not_empty = self.handle_port_supply_buy()
            next_reset = self._os_shop_delay(not_empty)
            logger.info('OS port daily finished, delay to next reset')
            logger.attr('OpsiShopNextReset', next_reset)
        else:
            next_reset = get_os_next_reset()
            logger.warning('There is no shop in the port, skip to the next month.')
            logger.attr('OpsiShopNextReset', next_reset)

        self.port_shop_quit()
        self.port_quit()

        self.config.task_delay(target=next_reset)
        self.config.task_stop()

    def _os_shop_delay(self, not_empty) -> datetime:
        """
        Calculate the delay of OpsiShop.

        Args:
            not_empty (bool): Indicates whether the shop is not empty.

        Returns:
            datetime: The time of the next shop reset.
        """
        next_reset = None

        if not_empty:
            next_reset = get_server_next_update(self.config.Scheduler_ServerUpdate)
        else:
            remain = get_os_reset_remain()
            next_reset = get_os_next_reset()
            if remain == 0:
                next_reset = get_server_next_update(self.config.Scheduler_ServerUpdate)
            elif remain < 7:
                next_reset = next_reset - timedelta(days=1)
            else:
                next_reset = (
                    get_server_next_update(self.config.Scheduler_ServerUpdate) +
                    timedelta(days=6)
                )
        return next_reset
