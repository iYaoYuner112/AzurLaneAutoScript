"""资源采集器（Resource Collector）。

导航到大世界，复用现有 OCR 读取资源，提交给 ResourceMonitor。只采集大世界侧资源：
行动力 / 黄币 / 紫币。

设计要点：
- 只在主线安全点被调用（不做后台 OCR 线程——device 非线程安全）；
- 复用 OperationSiren 的现有 OCR，不重复实现识别；
- OCR 失败（读不到 / 数值异常）时提交 None，由 ResourceMonitor 保留旧值；
- **按需点名**：行动力必须打开行动力面板才能读，所以没被点名就不开面板。任务流程自己
  读行动力时会顺路上报看板（`action_point_safe_get` → `record_dashboard_resource`），
  看板上的值还新鲜就没必要为监视器多开一次窗（对齐 AzurPilot 的 `scheduler_refresh`
  只读任务卡片要求的资源）。
"""

from module.logger import logger
from module.statistics.resource_monitor import (
    RESOURCE_ACTION_POINT,
    RESOURCE_PURPLE_COIN,
    RESOURCE_YELLOW_COIN,
    dashboard_resource_age,
    ResourceMonitor,
)


class ResourceCollector:
    #: 不用打开行动力面板就能读的项
    CHEAP_RESOURCES = (RESOURCE_YELLOW_COIN, RESOURCE_PURPLE_COIN)
    #: 看板存储用的键名（`record_dashboard_resource` 写的那套大小写）
    STORAGE_KEYS = {
        RESOURCE_ACTION_POINT: 'ActionPoint',
        RESOURCE_YELLOW_COIN: 'YellowCoin',
        RESOURCE_PURPLE_COIN: 'PurpleCoin',
    }

    def __init__(self, config, device, monitor: ResourceMonitor):
        self.config = config
        self.device = device
        self.monitor = monitor

    @classmethod
    def refresh_names(cls, config, interval_seconds, now=None):
        """本轮该读哪些资源。

        Args:
            config: Alas 配置，用来查看板上行动力的最后更新时间。
            interval_seconds (int): 监视器的刷新间隔（秒）。看板上的行动力比它还旧，
                说明任务流程很久没读过行动力了，这时才值得为监视器开一次面板。
            now (datetime): 注入当前时间，便于测试。

        Returns:
            set[str]: 要读的资源名。
        """
        names = set(cls.CHEAP_RESOURCES)
        age = dashboard_resource_age(config, cls.STORAGE_KEYS[RESOURCE_ACTION_POINT], now=now)
        if age is None or age >= interval_seconds:
            names.add(RESOURCE_ACTION_POINT)
        return names

    def refresh(self, names=None):
        """按需读取并提交资源；没点名行动力就不会打开行动力面板。

        Args:
            names (set[str] | None): 要读的项，取值 `RESOURCE_ACTION_POINT` /
                `RESOURCE_YELLOW_COIN` / `RESOURCE_PURPLE_COIN`。为 None 时只读
                不用开面板的那几项。

        Pages:
            in: 任意安全点
            out: page_os
        """
        from module.os.config import OSConfig
        from module.os.operation_siren import OperationSiren
        from module.ui.page import page_os

        wanted = set(self.CHEAP_RESOURCES) if names is None else set(names)
        if not wanted:
            return

        config = self.config.merge(OSConfig())
        ui = OperationSiren(config=config, device=self.device)
        if not ui.ui_page_appear(page_os):
            ui.ui_ensure(page_os)

        read = []
        if RESOURCE_YELLOW_COIN in wanted:
            yellow = ui.get_yellow_coins()
            self.monitor.submit(RESOURCE_YELLOW_COIN, yellow if yellow >= 100 else None,
                                source='collector')
            read.append(f'yellow_coin={yellow}')
        if RESOURCE_PURPLE_COIN in wanted:
            purple = ui.get_purple_coins()
            self.monitor.submit(RESOURCE_PURPLE_COIN, purple if purple >= 100 else None,
                                source='collector')
            read.append(f'purple_coin={purple}')
        if RESOURCE_ACTION_POINT in wanted:
            # 唯一需要打开行动力面板的一项
            action_point = ui._read_current_action_point()
            self.monitor.submit(RESOURCE_ACTION_POINT, action_point if action_point > 0 else None,
                                source='collector')
            read.append(f'action_point={action_point}')

        logger.info(f'[ResourceMonitor] scan: {", ".join(read)}')
