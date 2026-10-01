"""资源采集器（Resource Collector）。

导航到大世界、复用现有 OCR 读取资源、提交给 ResourceMonitor。
只采集大世界侧资源：行动力 / 黄币 / 紫币。

设计要点：
- 只在主线安全点被调用（不做后台 OCR 线程——device 非线程安全）；
- 复用 OperationSiren 的现有 OCR，不重复实现识别；
- OCR 失败（读不到 / 数值异常）时提交 None，由 ResourceMonitor 保留旧值。
"""

from module.logger import logger
from module.statistics.resource_monitor import (
    RESOURCE_ACTION_POINT,
    RESOURCE_PURPLE_COIN,
    RESOURCE_YELLOW_COIN,
    ResourceMonitor,
)


class ResourceCollector:
    def __init__(self, config, device, monitor: ResourceMonitor):
        self.config = config
        self.device = device
        self.monitor = monitor

    def refresh(self):
        """导航到大世界，读行动力 / 黄币 / 紫币并提交。

        调用前应在主线安全点（如游戏空闲在主界面）。
        """
        from module.os.config import OSConfig
        from module.os.operation_siren import OperationSiren
        from module.ui.page import page_os

        config = self.config.merge(OSConfig())
        ui = OperationSiren(config=config, device=self.device)
        if not ui.ui_page_appear(page_os):
            ui.ui_ensure(page_os)

        yellow = ui.get_yellow_coins()
        purple = ui.get_purple_coins()
        action_point = ui._read_current_action_point()

        self.monitor.submit(RESOURCE_YELLOW_COIN, yellow if yellow >= 100 else None, source='collector')
        self.monitor.submit(RESOURCE_PURPLE_COIN, purple if purple >= 100 else None, source='collector')
        self.monitor.submit(RESOURCE_ACTION_POINT, action_point if action_point > 0 else None, source='collector')
        logger.info(f'[ResourceMonitor] scan: action_point={action_point}, yellow_coin={yellow}, purple_coin={purple}')
