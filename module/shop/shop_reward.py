from module.logger import logger
from module.shop.assets import *
from module.shop.shop_core import CoreShop_250814
from module.shop.shop_general import GeneralShop_250814
from module.shop.shop_guild import GuildShop_250814
from module.shop.shop_medal import MedalShop2_250814
from module.shop.shop_merit import MeritShop_250814
from module.shop.ui import ShopUI


class RewardShop(ShopUI):
    """
    Runs the reward shops. Each shop has its own `Enable` switch, so a shop that is
    turned off is skipped instead of being walked into and browsed.
    """

    def run_frequent(self):
        self.ui_goto_shop()
        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_GENERAL, main=self)
        self.shop_tab_250814.set(TAB_GENERAL, main=self)
        if self.config.GeneralShop_Enable:
            GeneralShop_250814(self.config, self.device).run()
        else:
            logger.info('GeneralShop is disabled, skip')

        self.config.task_delay(server_update=True)

    def run_once(self):
        # Munitions shops
        self.ui_goto_shop()
        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_GENERAL, main=self)
        self.shop_tab_250814.set(TAB_MERIT, main=self)
        if self.config.MeritShop_Enable:
            MeritShop_250814(self.config, self.device).run()
        else:
            logger.info('MeritShop is disabled, skip')

        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_GENERAL, main=self)
        self.shop_tab_250814.set(TAB_GUILD, main=self)
        if self.config.GuildShop_Enable:
            GuildShop_250814(self.config, self.device).run()
        else:
            logger.info('GuildShop is disabled, skip')

        # core limited, core monthly, medal, prototype
        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_MONTHLY, main=self)
        self.shop_tab_250814.set(TAB_CORE_MONTHLY, main=self)
        if self.config.CoreShop_Enable:
            CoreShop_250814(self.config, self.device).run()
        else:
            logger.info('CoreShop is disabled, skip')

        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_MONTHLY, main=self)
        self.shop_tab_250814.set(TAB_MEDAL, main=self)
        if self.config.MedalShop2_Enable:
            MedalShop2_250814(self.config, self.device).run()
        else:
            logger.info('MedalShop2 is disabled, skip')

        self.config.task_delay(server_update=True)


if __name__ == '__main__':
    self = RewardShop('alas')
    self.device.screenshot()
    self.run_once()
