"""本轮可靠性增强的纯逻辑单测：stale 接口、塞壬装置状态机、防死循环守卫。

覆盖：
- invalidate_map_state -> ensure_map_state_current 做 FULL RESCAN 并清 stale；
- 装置子状态 + 中断恢复时重新观察 UI（不 replay stale click）；
- AntiLoopGuard 防死循环保险丝。
"""

from types import SimpleNamespace

from module.config.config import OS_MAP_STALE_KEY
from module.os.fixed_patrol import (
    DEVICE_COMPLETED,
    DEVICE_DIALOG_OPEN,
    DEVICE_INTERRUPTIBLE_STATES,
    DEVICE_NONE,
    DEVICE_TARGETED,
    AntiLoopGuard,
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
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    assert stub.config.values[OS_MAP_STALE_KEY] is False


# ---- 防死循环守卫 ----

def test_anti_loop_guards_no_progress():
    guard = AntiLoopGuard(max_repeats=3)
    assert not guard.check(None, None, 1, 'move')
    assert not guard.check(None, None, 1, 'move')
    assert not guard.check(None, None, 1, 'move')
    assert guard.check(None, None, 1, 'move')


def test_anti_loop_resets_on_state_change():
    guard = AntiLoopGuard(max_repeats=3)
    for _ in range(3):
        assert not guard.check(None, None, 1, 'move')
    assert not guard.check(None, None, 2, 'move')


def test_anti_loop_reset_method_clears_state():
    guard = AntiLoopGuard(max_repeats=3)
    for _ in range(4):
        guard.check(None, None, 1, 'move')
    guard.reset()
    assert guard.repeat_count == 0
    assert not guard.check(None, None, 1, 'move')
