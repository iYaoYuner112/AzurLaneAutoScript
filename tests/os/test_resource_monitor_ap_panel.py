"""资源监视器不再为读行动力而多开一次行动力面板（按需点名，对齐 AzurPilot）。

任务流程每次真读行动力都会顺路上报看板（`action_point.py:191` →
`record_dashboard_resource`），所以看板上的行动力还新鲜时，周期刷新只需要读不用开面板的
黄币/紫币；只有行动力已经过期（说明很久没跑过大世界任务）才值得为监视器开一次面板。
"""

from datetime import datetime, timedelta

from module.statistics.resource_collector import ResourceCollector
from module.statistics.resource_monitor import (
    RESOURCE_ACTION_POINT,
    RESOURCE_PURPLE_COIN,
    RESOURCE_YELLOW_COIN,
    dashboard_resource_age,
)


class ConfigStub:
    """只要 cross_get / merge 两个行为。"""

    def __init__(self, storage):
        self.storage = storage

    def cross_get(self, keys, default=None):
        return self.storage

    def merge(self, other):
        return self


def storage_with_action_point(seconds_ago, now=None):
    now = now or datetime.now()
    return {'ActionPoint': {'Value': 120,
                            'Record': (now - timedelta(seconds=seconds_ago)).strftime('%Y-%m-%d %H:%M:%S')}}


def test_age_missing_or_broken_record_returns_none():
    assert dashboard_resource_age(ConfigStub({}), 'ActionPoint') is None
    assert dashboard_resource_age(ConfigStub({'ActionPoint': {}}), 'ActionPoint') is None
    assert dashboard_resource_age(ConfigStub('not a dict'), 'ActionPoint') is None


def test_age_measured_against_given_now():
    now = datetime(2026, 10, 5, 12, 0, 0)
    config = ConfigStub(storage_with_action_point(90, now=now))
    assert dashboard_resource_age(config, 'ActionPoint', now=now) == 90


def test_fresh_dashboard_skips_the_action_point_panel():
    """看板上的行动力比刷新间隔还新 -> 本轮不点名行动力。"""
    now = datetime(2026, 10, 5, 12, 0, 0)
    config = ConfigStub(storage_with_action_point(60, now=now))
    names = ResourceCollector.refresh_names(config, interval_seconds=600, now=now)
    assert names == {RESOURCE_YELLOW_COIN, RESOURCE_PURPLE_COIN}


def test_stale_or_missing_dashboard_asks_for_action_point():
    now = datetime(2026, 10, 5, 12, 0, 0)
    stale = ConfigStub(storage_with_action_point(3600, now=now))
    assert RESOURCE_ACTION_POINT in ResourceCollector.refresh_names(stale, 600, now=now)
    never = ConfigStub({})
    assert RESOURCE_ACTION_POINT in ResourceCollector.refresh_names(never, 600, now=now)


class UiStub:
    """替 OperationSiren：记录读了哪些资源，`_read_current_action_point` 代表开面板。"""

    calls = []

    def __init__(self, config=None, device=None):
        pass

    def ui_page_appear(self, page):
        return True

    def ui_ensure(self, page):
        pass

    def get_yellow_coins(self):
        UiStub.calls.append('yellow_coin')
        return 5000

    def get_purple_coins(self):
        UiStub.calls.append('purple_coin')
        return 500

    def _read_current_action_point(self):
        UiStub.calls.append('action_point_panel')
        return 120


class MonitorStub:
    def __init__(self):
        self.submitted = []

    def submit(self, name, value, source='ocr', **kwargs):
        self.submitted.append((name, value))
        return None


def run_refresh(names):
    import module.os.operation_siren as os_mod
    saved = os_mod.OperationSiren
    os_mod.OperationSiren = UiStub
    UiStub.calls = []
    monitor = MonitorStub()
    try:
        ResourceCollector(config=ConfigStub({}), device=None, monitor=monitor).refresh(names=names)
    finally:
        os_mod.OperationSiren = saved
    return monitor


def test_refresh_without_action_point_never_opens_the_panel():
    monitor = run_refresh({RESOURCE_YELLOW_COIN, RESOURCE_PURPLE_COIN})
    assert UiStub.calls == ['yellow_coin', 'purple_coin']
    assert sorted(name for name, _ in monitor.submitted) == [RESOURCE_PURPLE_COIN, RESOURCE_YELLOW_COIN]


def test_refresh_with_action_point_opens_it_once():
    monitor = run_refresh({RESOURCE_YELLOW_COIN, RESOURCE_PURPLE_COIN, RESOURCE_ACTION_POINT})
    assert UiStub.calls == ['yellow_coin', 'purple_coin', 'action_point_panel']
    assert (RESOURCE_ACTION_POINT, 120) in monitor.submitted


def test_refresh_with_empty_names_does_nothing():
    monitor = run_refresh(set())
    assert UiStub.calls == []
    assert monitor.submitted == []
