"""Opsi 任务被其他任务抢占后的地图状态恢复（Resume Barrier）单测。

核心原则：只要 Opsi 任务被真正抢占过，恢复时就不能再信任抢占前的地图缓存，
必须走一次明确的恢复屏障——FULL RESCAN + 失效旧 solved 状态 + 清除 stale 标记。

覆盖：
- task_switched 只在「Opsi 任务被切走」时标记地图 stale；
- resume barrier 只做一次 FULL RESCAN，并失效旧 cache、清标记；
- 标记清除后不会反复触发 rescan（防每个循环都扫 / 防连续两次抢占重复扫）。
"""

from datetime import datetime

from types import SimpleNamespace

from module.config.config import AzurLaneConfig, Function, OS_MAP_STALE_KEY
from module.os.fixed_patrol import DEVICE_NONE
from module.os.map import OSMap


def make_function(command):
    return Function({
        'Scheduler': {
            'Command': command,
            'Enable': True,
            'NextRun': datetime(2026, 1, 1),
        }
    })


class FakeConfig:
    """只提供 task_switched 需要的属性。"""

    def __init__(self, prev_command, next_command):
        self.stop_event = None
        self.task = make_function(prev_command)
        self.next = make_function(next_command)
        self.cross_set_calls = []

    def load(self):
        pass

    def get_next(self):
        return self.next

    def cross_set(self, key, value):
        self.cross_set_calls.append((key, value))


# ---- task_switched 标记 stale ----

def test_switch_away_from_opsi_marks_map_stale():
    cfg = FakeConfig('OpsiScheduling', 'Commission')
    assert AzurLaneConfig.task_switched(cfg) is True
    assert (OS_MAP_STALE_KEY, True) in cfg.cross_set_calls


def test_switch_away_from_non_opsi_does_not_mark():
    cfg = FakeConfig('Commission', 'Research')
    assert AzurLaneConfig.task_switched(cfg) is True
    assert cfg.cross_set_calls == []


def test_continue_task_does_not_mark():
    cfg = FakeConfig('OpsiScheduling', 'OpsiScheduling')
    assert AzurLaneConfig.task_switched(cfg) is False
    assert cfg.cross_set_calls == []


# ---- resume barrier ----

class StatefulConfig:
    """记录 stale 标记的配置替身。"""

    def __init__(self, stale):
        self.stale = stale

    def cross_get(self, key, default=False):
        return self.stale

    def cross_set(self, key, value):
        self.stale = value


def make_resume_stub(stale):
    stub = SimpleNamespace(
        config=StatefulConfig(stale),
        zone=SimpleNamespace(zone_id=22),
        _solved_map_event={'is_akashi'},
        _solved_fleet_mechanism=True,
        rescan_calls=[],
        map_rescan=lambda **kw: None,
    )
    # Bind the real OSMap helpers used by ensure_map_state_current().
    stub._os_map_was_interrupted = lambda: OSMap._os_map_was_interrupted(stub)
    stub._device_state = DEVICE_NONE
    return stub


def test_os_map_was_interrupted_reads_flag():
    assert OSMap._os_map_was_interrupted(make_resume_stub(True)) is True
    assert OSMap._os_map_was_interrupted(make_resume_stub(False)) is False


def test_ensure_map_state_current_does_full_rescan_and_invalidates():
    stub = make_resume_stub(True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    assert stub._solved_map_event == set()
    assert stub._solved_fleet_mechanism is False


def test_ensure_map_state_current_clears_stale_flag():
    stub = make_resume_stub(True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.config.stale is False


def test_resume_barrier_runs_once_per_interruption():
    """恢复屏障只做一次：清了标记后，后续进入不会再触发 rescan。"""
    stub = make_resume_stub(True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert len(stub.rescan_calls) == 1
    # 标记已清除，第二次进入不会再次 FULL RESCAN
    assert OSMap._os_map_was_interrupted(stub) is False
