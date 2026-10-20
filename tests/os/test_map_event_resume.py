"""大世界事件跨重启续查（搬自 AzurPilot 的 module/os/map_event_resume.py）。

续查记录写在隐藏配置项 OpsiHazard1Leveling.OpsiHazard1Leveling.PendingMapEvent，
重启后先补完"已经看见、但没处理完"的事件。这里只测记录的读写与有效性判定，
恢复流程本身的行为由 test_siren_device_rounds.py 等用例覆盖。
"""

from types import SimpleNamespace

from module.base.utils import location2node
from module.config.utils import filepath_args, get_os_next_reset, read_file
from module.os.map_event_resume import (
    PENDING_MAP_EVENT_KEY,
    MapEventResumeMixin,
    _PENDING_MAP_EVENT_VERSION,
)


class ConfigStub:
    """只实现续查用到的 cross_get / cross_set，值存在字典里。"""

    SERVER = 'cn'

    def __init__(self):
        self.data = {}
        self.task = SimpleNamespace(command='OpsiHazard1Leveling')

    def cross_get(self, keys, default=None):
        return self.data.get(keys, default)

    def cross_set(self, keys, value):
        self.data[keys] = value


class Tracker(MapEventResumeMixin):
    def __init__(self, zone_id=22, server='cn'):
        self.config = ConfigStub()
        self.config.SERVER = server
        self.zone = SimpleNamespace(zone_id=zone_id)
        self.fleet_selector = SimpleNamespace(get=lambda: 1)
        self.auto_searches = 0
        self.fleets_set = []
        self.rescan_solves = False
        self.patrol_solves = False

    def os_auto_search_run(self, drop=None):
        self.auto_searches += 1

    def fleet_set(self, fleet):
        self.fleets_set.append(fleet)

    # ---- 恢复流程用到的地图操作 ----
    _solved_map_event = set()
    _solved_fleet_mechanism = False
    _question_unreachable = False

    def map_rescan(self, rescan_mode='full', drop=None):
        self.rescan_modes = getattr(self, 'rescan_modes', []) + [rescan_mode]
        if self.rescan_solves:
            self._clear_pending_map_event()

    def execute_fixed_patrol_scan(self, override=False):
        self.patrol_overrides = getattr(self, 'patrol_overrides', []) + [override]
        if self.patrol_solves:
            self._clear_pending_map_event()
        return False


class GridStub:
    def __init__(self, location=(3, 4)):
        self.location = location


def record(**overrides):
    """一条字段齐全、当前有效的续查记录。"""
    state = {
        'version': _PENDING_MAP_EVENT_VERSION,
        'reset': get_os_next_reset().isoformat(),
        'server': 'cn',
        'zone': 22,
        'type': 'akashi',
        'node': 'D4',
        'grid': [3, 4],
        'mode': None,
        'submitted': False,
        'remaining': 0,
        'fleet': 1,
        'created': '2026-10-20T22:00:00',
    }
    state.update(overrides)
    return state


def test_save_then_read_then_finish_clears():
    tracker = Tracker()
    tracker._save_pending_map_event('akashi', GridStub())
    state = tracker._read_pending_map_event()
    assert state['type'] == 'akashi'
    assert state['node'] == location2node((3, 4))
    assert state['server'] == 'cn'
    assert state['zone'] == 22
    assert state['version'] == _PENDING_MAP_EVENT_VERSION
    assert tracker._load_pending_map_event() is not None

    tracker._finish_pending_map_event('akashi', node=state['node'])
    assert tracker._read_pending_map_event() is None


def test_finish_ignores_another_event():
    """记录的是别的格子时不能顺手清掉，否则会漏掉那只猫。"""
    tracker = Tracker()
    tracker._save_pending_map_event('akashi', GridStub())
    tracker._finish_pending_map_event('scanning_device')
    assert tracker._read_pending_map_event() is not None
    tracker._finish_pending_map_event('akashi', node='A1')
    assert tracker._read_pending_map_event() is not None


def test_save_is_idempotent_for_the_same_event():
    tracker = Tracker()
    tracker._save_pending_map_event('akashi', GridStub())
    first = dict(tracker._read_pending_map_event())
    tracker._save_pending_map_event('akashi', GridStub())
    assert tracker._read_pending_map_event() == first


def test_update_only_touches_existing_record():
    tracker = Tracker()
    tracker._update_pending_map_event(mode='enemy')
    assert tracker._read_pending_map_event() is None

    tracker._save_pending_map_event('scanning_device', GridStub())
    tracker._update_pending_map_event(mode='enemy', submitted=True, remaining=3)
    state = tracker._read_pending_map_event()
    assert state['mode'] == 'enemy' and state['submitted'] is True
    assert state['remaining'] == 3


def test_missing_record_is_not_an_error():
    tracker = Tracker()
    assert tracker._load_pending_map_event() is None
    tracker._record_siren_followup_progress()
    tracker._update_pending_map_event(mode='enemy')
    tracker._finish_pending_map_event('akashi')
    assert tracker._read_pending_map_event() is None


def test_stale_records_are_discarded():
    invalid = [
        record(version=_PENDING_MAP_EVENT_VERSION + 1),
        record(reset='2020-01-01T00:00:00'),
        record(server='jp'),
        record(zone=44),
        record(type='logging_tower'),
        record(node=''),
        record(mode='unknown'),
        record(remaining='3'),
        record(remaining=-1),
    ]
    for state in invalid:
        tracker = Tracker()
        tracker.config.data[PENDING_MAP_EVENT_KEY] = dict(state)
        assert tracker._load_pending_map_event() is None, state
        assert tracker._read_pending_map_event() is None, state


def test_valid_record_modes_survive():
    for mode in (None, 'enemy', 'resource', 'collected'):
        tracker = Tracker()
        tracker.config.data[PENDING_MAP_EVENT_KEY] = record(
            type='scanning_device', mode=mode, submitted=True, remaining=3)
        loaded = tracker._load_pending_map_event()
        assert loaded is not None, mode
        assert loaded['mode'] == mode


def test_followup_progress_decrements_but_not_below_zero():
    tracker = Tracker()
    tracker.config.data[PENDING_MAP_EVENT_KEY] = record(
        type='scanning_device', mode='enemy', submitted=True, remaining=2)
    tracker._record_siren_followup_progress()
    assert tracker._read_pending_map_event()['remaining'] == 1
    tracker._record_siren_followup_progress()
    tracker._record_siren_followup_progress()
    assert tracker._read_pending_map_event()['remaining'] == 0


def test_followup_run_count_follows_the_device_mode():
    tracker = Tracker()
    assert tracker._siren_followup_run_count('enemy') == 3
    assert tracker._siren_followup_run_count('resource') == 1
    assert tracker._siren_followup_run_count(None) == 1
    assert tracker._siren_followup_run_count('collected') == 0


def test_followup_search_runs_only_the_remaining_rounds():
    tracker = Tracker()
    tracker.config.data[PENDING_MAP_EVENT_KEY] = record(
        type='scanning_device', mode='enemy', submitted=True, remaining=1)
    tracker._run_siren_followup_search('enemy', remaining=1)
    assert tracker.auto_searches == 1
    assert tracker._read_pending_map_event()['remaining'] == 0


def test_followup_search_skips_collected_device():
    tracker = Tracker()
    tracker._run_siren_followup_search('collected')
    assert tracker.auto_searches == 0


def test_resume_of_submitted_device_runs_only_the_remaining_rounds():
    """装置已提交物品：重启后只补记录里剩下的轮数，补完清记录。"""
    tracker = Tracker()
    tracker.config.data[PENDING_MAP_EVENT_KEY] = record(
        type='scanning_device', mode='enemy', submitted=True, remaining=1)
    tracker._resume_pending_map_event()
    assert tracker.auto_searches == 1
    assert tracker._read_pending_map_event() is None
    assert getattr(tracker, 'rescan_modes', []) == []


def test_resume_scans_the_map_before_giving_up():
    """未提交的明石：先全图扫描；扫到并处理完就停，不去挪舰队。"""
    tracker = Tracker()
    tracker.config.data[PENDING_MAP_EVENT_KEY] = record()
    tracker.rescan_solves = True
    tracker._resume_pending_map_event()
    assert tracker.rescan_modes == ['full']
    assert getattr(tracker, 'patrol_overrides', []) == []
    assert tracker._read_pending_map_event() is None


def test_resume_escalates_to_forced_patrol():
    """全图扫描没找到：升级强制移动，且不受"强制移动"开关限制。"""
    tracker = Tracker()
    tracker.config.data[PENDING_MAP_EVENT_KEY] = record()
    tracker.patrol_solves = True
    tracker._resume_pending_map_event()
    assert tracker.patrol_overrides == [True]
    assert tracker._question_unreachable is True
    assert tracker._read_pending_map_event() is None


def test_resume_clears_the_record_when_the_event_is_gone():
    """扫描和强制移动都看不到事件：清记录继续下一轮，不把"没找到"当卡死。"""
    tracker = Tracker()
    tracker.config.data[PENDING_MAP_EVENT_KEY] = record()
    tracker._resume_pending_map_event()
    assert tracker.patrol_overrides == [True]
    assert tracker._read_pending_map_event() is None


def test_pending_event_field_is_hidden_and_stored():
    """配置项必须是 stored + hide：否则每次加载配置都会被抹回 null。"""
    meta = read_file(filepath_args())['OpsiHazard1Leveling']['OpsiHazard1Leveling']['PendingMapEvent']
    assert meta['type'] == 'stored'
    assert meta['display'] == 'hide'
    assert meta['value'] is None
