"""本轮三个技术债的纯逻辑单测：stale 接口、舰队选队接线、塞壬装置状态机。

覆盖 spec #25 的核心：
- Test A/B: choose_fleet_for_target 用真实位置选队（在 test_fixed_patrol_target 里另有 16 条）；
- Test C: invalidate_map_state -> ensure_map_state_current 做 FULL RESCAN 并清 stale；
- Test D/E/F: 装置子状态 + 中断恢复时重新观察 UI（不 replay stale click）。
"""

from types import SimpleNamespace

from module.config.config import OS_MAP_STALE_KEY
from module.os.fixed_patrol import (
    DEVICE_COMPLETED,
    DEVICE_DIALOG_OPEN,
    DEVICE_INTERRUPTIBLE_STATES,
    DEVICE_NONE,
    DEVICE_TARGETED,
    FixedPatrolTarget,
    choose_fleet_for_target,
)
from module.os.map import DEVICE_STATE_KEY, OSMap


class DictConfig:
    """按 key 存取的最小配置替身。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.set_calls = []

    def cross_get(self, key, default=None):
        return self.values.get(key, default)

    def cross_set(self, key, value):
        self.values[key] = value
        self.set_calls.append((key, value))


def make_map_stub(values):
    """构造带真实 OSMap 恢复 helper 的替身。"""
    stub = SimpleNamespace(
        config=DictConfig(values),
        zone=SimpleNamespace(zone_id=22),
        _solved_map_event=set(),
        _solved_fleet_mechanism=False,
        rescan_calls=[],
    )
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    stub._os_map_was_interrupted = lambda: OSMap._os_map_was_interrupted(stub)
    stub._device_state = OSMap._device_state.fget(stub)
    return stub


# ---- stale 接口 ----

def test_invalidate_map_state_sets_flag():
    cfg = DictConfig()
    stub = SimpleNamespace(config=cfg)
    OSMap.invalidate_map_state(stub, 'TASK_INTERRUPTION')
    assert cfg.values[OS_MAP_STALE_KEY] is True


def test_ensure_map_state_current_resyncs_and_clears_flag():
    stub = make_map_stub({OS_MAP_STALE_KEY: True})
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    assert stub.config.values[OS_MAP_STALE_KEY] is False


def test_ensure_map_state_current_skips_when_not_stale():
    stub = make_map_stub({OS_MAP_STALE_KEY: False})
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == []


# ---- 装置状态机 ----

def test_device_state_roundtrip():
    cfg = DictConfig()
    stub = SimpleNamespace(config=cfg)
    assert OSMap._device_state.fget(stub) == DEVICE_NONE
    OSMap._set_device_state(stub, DEVICE_TARGETED)
    assert OSMap._device_state.fget(stub) == DEVICE_TARGETED


def test_interruptible_device_states():
    assert DEVICE_TARGETED in DEVICE_INTERRUPTIBLE_STATES
    assert DEVICE_DIALOG_OPEN in DEVICE_INTERRUPTIBLE_STATES
    assert DEVICE_COMPLETED not in DEVICE_INTERRUPTIBLE_STATES
    assert DEVICE_NONE not in DEVICE_INTERRUPTIBLE_STATES


def test_ensure_map_state_current_reexamines_interrupted_device():
    """弹窗打开时被抢占 -> 恢复时 FULL RESCAN（重新观察 UI），而非回放过期点击。"""
    stub = make_map_stub({OS_MAP_STALE_KEY: True, DEVICE_STATE_KEY: DEVICE_DIALOG_OPEN})
    OSMap.ensure_map_state_current(stub)
    # 只要 stale，无论装置状态如何，都会做一次 FULL RESCAN 重新观察
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    assert stub.config.values[OS_MAP_STALE_KEY] is False


# ---- 舰队选队接线 ----

def test_fixed_patrol_fleets_uses_recorded_positions():
    stub = SimpleNamespace(_fixed_patrol_fleet_positions={1: (5, 5), 2: None, 3: (1, 1), 4: None})
    fleets = OSMap._fixed_patrol_fleets(stub)
    locations = [f.location for f in fleets]
    assert locations == [(5, 5), None, (1, 1), None]


def test_choose_fleet_for_target_switches_when_positions_change():
    """spec #25 Test A：位置改变后应改选最近舰队，而不是固定返回同一支。"""
    target = FixedPatrolTarget('AKASHI_SHOP', (2, 1))
    from module.os.fixed_patrol import FixedPatrolFleet
    # 初始：Fleet1 最近
    fleets = [FixedPatrolFleet(1, (1, 1)), FixedPatrolFleet(2, (10, 10))]
    assert choose_fleet_for_target(target, fleets).index == 1
    # 位置改变后：Fleet2 最近
    fleets = [FixedPatrolFleet(1, (20, 20)), FixedPatrolFleet(2, (3, 1))]
    assert choose_fleet_for_target(target, fleets).index == 2
