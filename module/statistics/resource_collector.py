"""资源采集器（Resource Collector）。

导航到对应页面、复用现有 OCR 读取资源、提交给 ResourceMonitor。

设计要点：
- 只在主线安全点被调用（不做后台 OCR 线程——device 非线程安全）；
- 复用 CampaignStatus / OperationSiren 的现有 OCR，不重复实现识别；
- OCR 失败（读不到 / 数值异常）时提交 None，由 ResourceMonitor 保留旧值。
"""

from module.logger import logger
from module.statistics.resource_monitor import (
    RESOURCE_COIN,
    RESOURCE_PURPLE_COIN,
    RESOURCE_YELLOW_COIN,
    ResourceMonitor,
)


class ResourceCollector:
    def __init__(self, config, device, monitor: ResourceMonitor):
        self.config = config
        self.device = device
        self.monitor = monitor

    def refresh_main(self):
        """导航到主界面，读 Coin（物资）并提交。

        调用前应在主线安全点（如游戏空闲在主界面）。
        """
        from module.campaign.campaign_status import CampaignStatus
        from module.ui.page import page_main

        ui = CampaignStatus(self.config, self.device)
        if not ui.ui_page_appear(page_main):
            ui.ui_ensure(page_main)

        coin = ui.get_coin()

        # get_coin 失败返回 0，当 None 提交，保留旧值。
        self.monitor.submit(RESOURCE_COIN, coin if coin >= 100 else None, source='collector')
        logger.info(f'[ResourceMonitor] scan: coin={coin}')

    def refresh_os(self):
        """导航到大世界，读黄币 / 紫币并提交。"""
        from module.os.config import OSConfig
        from module.os.operation_siren import OperationSiren
        from module.ui.page import page_os

        config = self.config.merge(OSConfig())
        ui = OperationSiren(config=config, device=self.device)
        if not ui.ui_page_appear(page_os):
            ui.ui_ensure(page_os)

        yellow = ui.get_yellow_coins()
        purple = ui.get_purple_coins()

        self.monitor.submit(RESOURCE_YELLOW_COIN, yellow if yellow >= 100 else None, source='collector')
        self.monitor.submit(RESOURCE_PURPLE_COIN, purple if purple >= 100 else None, source='collector')
        logger.info(f'[ResourceMonitor] scan: yellow_coin={yellow}, purple_coin={purple}')
