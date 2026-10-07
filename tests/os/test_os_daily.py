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


# ---- 接取端：任务槽被同名研究任务占满时必须算成功，否则 os_daily 会无限重接 ----

from module.os_handler.assets import MISSION_OVERVIEW_EMPTY
from module.os_handler.mission import MissionHandler


class OverviewAcceptStub:
    """只喂 `os_mission_overview_accept` 用到的那几个界面判定。"""

    os_mission_overview_accept = MissionHandler.os_mission_overview_accept

    def __init__(self, info_bar=0, empty=False):
        self.info_bar = info_bar
        self.empty = empty
        self.interval_timer = {}

    def loop(self, *args, **kwargs):
        for _ in range(5):
            yield

    def appear(self, button, offset=0, interval=0, **kwargs):
        return button is MISSION_OVERVIEW_EMPTY and self.empty

    def appear_then_click(self, button, offset=0, interval=0, **kwargs):
        return False

    def handle_manjuu(self):
        return False

    def is_in_globe(self):
        return True

    def info_bar_count(self):
        return self.info_bar

    def ui_click(self, *args, **kwargs):
        pass

    def ui_back(self, *args, **kwargs):
        pass

    def os_map_goto_globe(self, *args, **kwargs):
        pass

    def os_globe_goto_map(self, *args, **kwargs):
        pass


def test_slots_full_counts_as_success_when_siren_research_is_skipped():
    """开着跳过时"接不进任务"是预期状态：返回 True，os_daily 才会收工而不是原地重接。"""
    assert OverviewAcceptStub(info_bar=1).os_mission_overview_accept(skip_siren_mission=True) is True


def test_slots_full_still_counts_as_failure_without_the_skip():
    assert OverviewAcceptStub(info_bar=1).os_mission_overview_accept(skip_siren_mission=False) is False


def test_empty_overview_accepts_cleanly():
    assert OverviewAcceptStub(info_bar=0, empty=True).os_mission_overview_accept(
        skip_siren_mission=True) is True
