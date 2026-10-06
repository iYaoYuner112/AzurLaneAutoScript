"""总览页的资源看板存储。

任务在顺手读到某个资源时调用 `record_dashboard_resource()`，把值写进
`Alas.Storage.Storage.ResourceMonitor`；总览页按 `DASHBOARD_RESOURCES` 这份目录
渲染卡片，前端不写死资源列表。

没有定时采集：某项什么时候更新，取决于哪个流程读到了它，卡片上的时间戳就是
最后一次上报的时刻，超过一天没动会置灰。仓库里读到这些数的地方是主线/委托结算
（石油、物资、活动PT）、行动力面板（行动力）、大世界状态（黄币、紫币）和商店（钻石、
勋章、功勋、核心数据）。
"""

from datetime import datetime
from typing import NamedTuple

RESOURCE_STORAGE_PATH = 'Alas.Storage.Storage.ResourceMonitor'
# 同一个资源至少间隔这么久才重写一次，避免顺路读取把配置刷个不停
RESOURCE_UPDATE_INTERVAL_SECONDS = 20


class DashboardResource(NamedTuple):
    """总览页一张资源卡片需要的展示信息。

    Attributes:
        key (str): `Alas.Storage.Storage.ResourceMonitor` 里的记录名，
            即 `record_dashboard_resource()` 用的那个名字。
        label (str): i18n 词条名，前端取 `Gui.Overview.<label>`。
        icon (str): `assets/gui/icon/resource/<icon>.png`，图标直接复用
            AzurPilot 前端自带的美术资源，等比缩小不改图形。
        shows_total (bool): 这项资源有没有「当前 / 上限」两段数字，
            目前只有行动力带 Total（含行动力箱的总行动力）。
    """

    key: str
    label: str
    icon: str
    shows_total: bool = False


# 总览页的资源目录，顺序就是卡片顺序（照参考图排）。
# 前端只按这份目录渲染：以后要加资源，在这里加一行 + 有读取处上报即可。
# 心智魔方、舰队币没列进来是因为仓库里目前没有任何地方能读到它们的数。
DASHBOARD_RESOURCES = (
    DashboardResource('Gems', 'ResourceGems', 'gems'),
    DashboardResource('Oil', 'ResourceOil', 'oil'),
    DashboardResource('Coin', 'ResourceCoin', 'coin'),
    DashboardResource('ActionPoint', 'ResourceActionPoint', 'action_point',
                      shows_total=True),
    DashboardResource('EventPT', 'ResourceEventPT', 'event_pt'),
    DashboardResource('YellowCoin', 'ResourceYellowCoin', 'yellow_coin'),
    DashboardResource('PurpleCoin', 'ResourcePurpleCoin', 'purple_coin'),
    DashboardResource('Core', 'ResourceCore', 'core_data'),
    DashboardResource('Medal', 'ResourceMedal', 'medal'),
    DashboardResource('Merit', 'ResourceMerit', 'merit'),
)


def record_dashboard_resource(config, name, value, total=None, limit=None, now=None):
    try:
        value = int(value)
        total = int(total) if total is not None else None
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        return False

    now = now or datetime.now()
    resources = config.cross_get(RESOURCE_STORAGE_PATH, default={})
    if not isinstance(resources, dict):
        resources = {}

    previous = resources.get(name, {})
    try:
        previous_time = datetime.strptime(previous.get('Record', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        previous_time = None
    if previous_time and (now - previous_time).total_seconds() < RESOURCE_UPDATE_INTERVAL_SECONDS:
        return False

    record = {
        'Value': value,
        'Record': now.strftime('%Y-%m-%d %H:%M:%S'),
    }
    if total is not None:
        record['Total'] = total
    if limit is not None:
        record['Limit'] = limit
    # Delta since the last recorded value of this resource, for the dashboard.
    prev_value = previous.get('Value')
    if isinstance(prev_value, int):
        record['Delta'] = value - prev_value
    resources[name] = record
    config.modified[RESOURCE_STORAGE_PATH] = resources
    config.save()
    return True
