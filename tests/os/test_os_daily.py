"""大世界每日增强（跳过塞壬研究 / 保留海域）的纯逻辑单测。

覆盖：
- 配置默认值；
- os_daily_set_keep_mission_zone 保存/去重未清理海域；
- 塞壬研究跳过开关的配置读取。
"""

from types import SimpleNamespace

from module.config.config import AzurLaneConfig
from module.os.tasks.daily import OpsiDaily


class FakeConfig:
    """记录 OpsiDaily_MissionZones 赋值的配置替身。"""

    def __init__(self, zones=None):
        self.OpsiDaily_MissionZones = zones

    def __setattr__(self, key, value):
        object.__setattr__(self, key, value)


def make_stub(zones=None, zone_id=10):
    return SimpleNamespace(
        config=FakeConfig(zones),
        zone=SimpleNamespace(zone_id=zone_id),
    )


def test_skip_siren_config_defaults():
    assert AzurLaneConfig.OpsiDaily_SkipSirenResearchMission is False
    assert AzurLaneConfig.OpsiDaily_KeepMissionZone is False
    assert AzurLaneConfig.OpsiDaily_OnlyPortDailyBeforeFullControl is False
    assert AzurLaneConfig.OpsiDaily_MissionZones is None


def test_set_keep_mission_zone_appends_new_zone():
    stub = make_stub(zones=None, zone_id=10)
    OpsiDaily.os_daily_set_keep_mission_zone(stub)
    assert stub.config.OpsiDaily_MissionZones == '10'


def test_set_keep_mission_zone_dedupes_existing_zone():
    stub = make_stub(zones='10 20', zone_id=10)
    OpsiDaily.os_daily_set_keep_mission_zone(stub)
    assert stub.config.OpsiDaily_MissionZones == '10 20'


def test_set_keep_mission_zone_appends_to_existing():
    stub = make_stub(zones='10', zone_id=30)
    OpsiDaily.os_daily_set_keep_mission_zone(stub)
    assert stub.config.OpsiDaily_MissionZones == '10 30'
