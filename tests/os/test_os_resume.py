"""Opsi 任务被其他任务抢占后的地图状态恢复（Resume Barrier）单测。

核心原则：只要 Opsi 任务被真正抢占过，恢复时就不能再信任抢占前的地图缓存，
必须失效旧 solved 状态并清掉 stale 标记。

覆盖：
- task_switched 只在「Opsi 任务被切走」时标记地图 stale；
- task_switched 额外在「OpsiScheduling 被切走」时留下一次性恢复标志；
- 普通 Opsi 任务恢复：走一次 FULL RESCAN；
- OpsiScheduling（智能调度）恢复：不做任何扫描或自律寻敌，地图交给子任务流程
  重建（侵蚀1练级自带计划作战 + clear_question + map_rescan）；
- os_init 对 OpsiScheduling 不再跑首次自律寻敌，只挂一次性标志，由调度层
  handle_first_auto_search() 决定是否补跑；
- 标记清除后不会反复触发（防每个循环都扫 / 防连续两次抢占重复扫）。
"""

from datetime import datetime

from types import SimpleNamespace

from module.config.config import (
    AzurLaneConfig, Function, OS_MAP_STALE_KEY, OS_RESUME_RECOVERY_KEY
)
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


# ---- task_switched 标记一次性恢复标志（OpsiScheduling 专属）----

def test_switch_away_from_opsi_scheduling_marks_resume_recovery():
    cfg = FakeConfig('OpsiScheduling', 'Commission')
    assert AzurLaneConfig.task_switched(cfg) is True
    assert (OS_RESUME_RECOVERY_KEY, True) in cfg.cross_set_calls


def test_switch_away_from_other_opsi_does_not_mark_resume_recovery():
    cfg = FakeConfig('OpsiDaily', 'Commission')
    assert AzurLaneConfig.task_switched(cfg) is True
    assert all(key != OS_RESUME_RECOVERY_KEY for key, _ in cfg.cross_set_calls)


def test_task_switched_uses_owner_when_a_child_is_proxied():
    """代理子任务期间必须以拥有者（调度器）做切换判断。

    `config.task` 此刻是子任务，直接拿它比较会把每一轮都误判成任务切换。
    """
    cfg = FakeConfig('OpsiHazard1Leveling', 'OpsiScheduling')
    cfg._task_switch_owner = make_function('OpsiScheduling')
    assert AzurLaneConfig.task_switched(cfg) is False
    assert cfg.cross_set_calls == []


def test_task_switched_without_owner_would_mistake_the_child_for_a_switch():
    """没有拥有者信息时，子任务身份确实会被当作切换——这正是要守护的契约。"""
    cfg = FakeConfig('OpsiHazard1Leveling', 'OpsiScheduling')
    assert AzurLaneConfig.task_switched(cfg) is True


# ---- resume barrier ----

class StatefulConfig:
    """记录 stale / recovery 标记的配置替身。"""

    def __init__(self, stale=False, command='OpsiScheduling', recovery=False):
        self.task = SimpleNamespace(command=command)
        self.store = {
            OS_MAP_STALE_KEY: stale,
            OS_RESUME_RECOVERY_KEY: recovery,
        }

    def cross_get(self, key, default=False):
        return self.store.get(key, default)

    def cross_set(self, key, value):
        self.store[key] = value

    def override(self, **kwargs):
        pass

    @property
    def stale(self):
        return self.store.get(OS_MAP_STALE_KEY, False)

    @property
    def recovery(self):
        return self.store.get(OS_RESUME_RECOVERY_KEY, False)


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
    stub._os_resume_recovery_available = lambda: OSMap._os_resume_recovery_available(stub)
    stub._device_state = DEVICE_NONE
    return stub


def make_scheduling_stub(stale=True, recovery=True, command='OpsiScheduling', zone_id=10):
    """带一次性恢复标志的替身：智能调度被其他任务抢占后恢复。"""
    stub = make_resume_stub(stale)
    stub.config = StatefulConfig(stale, command=command, recovery=recovery)
    stub.zone = SimpleNamespace(zone_id=zone_id)
    stub.auto_search_calls = []
    stub.run_auto_search = lambda **kw: stub.auto_search_calls.append(kw)
    return stub


def test_os_map_was_interrupted_reads_flag():
    assert OSMap._os_map_was_interrupted(make_resume_stub(True)) is True
    assert OSMap._os_map_was_interrupted(make_resume_stub(False)) is False


def test_ensure_map_state_current_invalidates_without_rescanning():
    """被打断后只作废缓存的扫描状态，不重扫整图（对齐 AzurPilot：AP 没有这一步）。"""
    stub = make_resume_stub(True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == []
    assert stub._solved_map_event == set()
    assert stub._solved_fleet_mechanism is False


def test_ensure_map_state_current_clears_stale_flag():
    stub = make_resume_stub(True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.config.stale is False


def test_resume_barrier_runs_once_per_interruption():
    """恢复屏障只做一次：清了标记后，后续进入不会再作废缓存。"""
    stub = make_resume_stub(True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.config.stale is False
    assert OSMap._os_map_was_interrupted(stub) is False
    # 第二次进入直接返回，不会把新一轮的扫描结果再清掉
    stub._solved_map_event = {'is_akashi'}
    OSMap.ensure_map_state_current(stub)
    assert stub._solved_map_event == {'is_akashi'}


# ---- 智能调度恢复：不重扫、不跑自律寻敌，交给子任务流程重建 ----

def test_scheduling_resume_neither_rescans_nor_runs_auto_search():
    """OpsiScheduling 恢复：不扫描、不寻敌，只失效缓存并清 stale 标记。

    自律寻敌会先把海域打空，让随后的计划作战无目标可打，地图事件也就不会
    在 map_rescan 里被处理——这正是「自律完直接进计划作战、事件被漏」的根因，
    所以恢复时不再跑它（对齐 AzurPilot）。
    """
    stub = make_scheduling_stub(stale=True, recovery=True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == []
    assert stub.auto_search_calls == []
    assert stub._solved_map_event == set()
    assert stub._solved_fleet_mechanism is False
    # 一次性标志已消费、stale 已清，屏障不会重复触发
    assert stub.config.recovery is False
    assert stub.config.stale is False


def test_scheduling_resume_does_not_repeat():
    stub = make_scheduling_stub(stale=True, recovery=True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == []
    assert stub.auto_search_calls == []


def test_scheduling_resume_applies_to_every_zone():
    for zone_id in (10, 22, 44, 154):
        stub = make_scheduling_stub(stale=True, recovery=True, zone_id=zone_id)
        stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
        OSMap.ensure_map_state_current(stub)
        assert stub.rescan_calls == [], f'zone {zone_id} must not rescan'
        assert stub.auto_search_calls == [], f'zone {zone_id} must not auto search'
        assert stub.config.stale is False


def test_ensure_ignores_recovery_flag_for_other_tasks():
    """恢复标志只在 OpsiScheduling 上消费；其他任务恢复同样不重扫。"""
    stub = make_scheduling_stub(stale=True, recovery=True, command='OpsiDaily')
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == []
    assert stub.config.stale is False
    # 非智能调度恢复不消费 recovery 标志
    assert stub.config.recovery is True


def test_ensure_other_opsi_resume_does_not_rescan():
    """普通 Opsi 抢占恢复（无 recovery 标志）：也只作废缓存，不重扫整图。"""
    stub = make_scheduling_stub(stale=True, recovery=False)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == []
    assert stub.auto_search_calls == []
    assert stub.config.stale is False


def test_recovery_available_requires_scheduling_command():
    """恢复标志检查：必须是 OpsiScheduling 且标志存在。"""
    stub = make_scheduling_stub(recovery=True, command='OpsiScheduling')
    assert OSMap._os_resume_recovery_available(stub) is True
    stub = make_scheduling_stub(recovery=True, command='OpsiDaily')
    assert OSMap._os_resume_recovery_available(stub) is False
    stub = make_scheduling_stub(recovery=False, command='OpsiScheduling')
    assert OSMap._os_resume_recovery_available(stub) is False


def test_case_no_interruption_adds_no_scan():
    """没被抢占（无 stale/recovery）→ 完全不动作。"""
    stub = make_scheduling_stub(stale=False, recovery=False)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == []
    assert stub.auto_search_calls == []


# ---- os_init：智能调度不再跑首次自律寻敌 ----

def make_os_init_stub(zone_id=10, command='OpsiScheduling', smart=True, recovery=False):
    """带完整 os_init 依赖的 stub，按调用顺序记录发生了什么。"""
    stub = make_scheduling_stub(
        stale=True, recovery=recovery, command=command, zone_id=zone_id)
    sequence = []
    stub.map_rescan = lambda **kw: sequence.append('full_rescan')
    stub.ensure_map_state_current = lambda: OSMap.ensure_map_state_current(stub)
    stub.is_in_map = lambda: True
    stub.is_in_globe = lambda: False
    stub.zone_init = lambda *a, **kw: None
    stub.hp_reset = lambda *a, **kw: None
    stub.handle_after_auto_search = lambda *a, **kw: sequence.append('after_auto_search')
    stub.handle_current_fleet_resolve = lambda *a, **kw: None
    stub.is_in_special_zone = lambda: False
    stub.handle_ash_beacon_attack = lambda *a, **kw: sequence.append('ash_beacon')
    stub.is_smart_scheduling_enabled = smart
    stub.run_first_auto_search = lambda: sequence.append('first_auto_search')
    return stub, sequence


def test_os_init_defers_first_auto_search_for_scheduling():
    """OpsiScheduling：不跑首次自律寻敌，只挂起标志交给调度层决定。"""
    for zone_id in (10, 22, 44, 154):
        stub, sequence = make_os_init_stub(zone_id=zone_id)
        OSMap.os_init(stub)
        assert 'first_auto_search' not in sequence, f'zone {zone_id} must defer'
        assert stub._smart_scheduling_first_auto_search_pending is True


def test_os_init_handles_ash_beacon_in_ash_zones():
    """22/44/154 仍要处理灰烬信标。"""
    for zone_id in (22, 44, 154):
        stub, sequence = make_os_init_stub(zone_id=zone_id)
        OSMap.os_init(stub)
        assert 'ash_beacon' in sequence, f'zone {zone_id} must still handle the ash beacon'
        assert 'first_auto_search' not in sequence


def test_os_init_runs_first_auto_search_for_other_tasks():
    """非智能调度任务：照旧跑首次自律寻敌。"""
    stub, sequence = make_os_init_stub(zone_id=10, command='OpsiDaily', smart=False)
    stub.config.store[OS_MAP_STALE_KEY] = False
    OSMap.os_init(stub)
    assert 'first_auto_search' in sequence
    assert stub._smart_scheduling_first_auto_search_pending is False


def test_os_init_skips_first_auto_search_when_scheduling_disabled():
    """任务名是 OpsiScheduling 但智能调度未启用：退回普通流程。"""
    stub, sequence = make_os_init_stub(zone_id=10, command='OpsiScheduling', smart=False)
    stub.config.store[OS_MAP_STALE_KEY] = False
    OSMap.os_init(stub)
    assert 'first_auto_search' in sequence
    assert stub._smart_scheduling_first_auto_search_pending is False
