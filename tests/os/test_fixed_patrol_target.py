"""固定巡逻（强制移动）目标驱动决策的纯逻辑单测。

覆盖：目标优先级、舰队选择、塞壬装置映射、防死循环守卫。
全部是纯 Python 逻辑，不依赖截图 / 点击 / 走路。
"""

from types import SimpleNamespace

from module.os.fixed_patrol import (
    AKASHI_SHOP,
    NORMAL_BATTLE,
    SIREN_PROBE,
    SIREN_INFORMATION_DEVICE,
    SPECIAL_RESOURCE,
    AntiLoopGuard,
    FixedPatrolFleet,
    FixedPatrolTarget,
    build_targets,
    choose_best_target,
    choose_fleet_for_target,
)


# ---- 目标优先级 ----

def test_choose_best_target_picks_highest_priority():
    targets = [
        FixedPatrolTarget(NORMAL_BATTLE, (0, 0)),
        FixedPatrolTarget(AKASHI_SHOP, (1, 1)),
        FixedPatrolTarget(SIREN_INFORMATION_DEVICE, (2, 2)),
        FixedPatrolTarget(SIREN_PROBE, (3, 3)),
    ]
    assert choose_best_target(targets).kind == SIREN_PROBE


def test_choose_best_target_skips_handled():
    targets = [
        FixedPatrolTarget(SIREN_PROBE, (3, 3), handled=True),
        FixedPatrolTarget(AKASHI_SHOP, (1, 1)),
    ]
    assert choose_best_target(targets).kind == AKASHI_SHOP


def test_choose_best_target_returns_none_when_all_handled():
    targets = [
        FixedPatrolTarget(SIREN_PROBE, (3, 3), handled=True),
        FixedPatrolTarget(AKASHI_SHOP, (1, 1), handled=True),
    ]
    assert choose_best_target(targets) is None


def test_choose_best_target_prefers_known_location_on_tie():
    targets = [
        FixedPatrolTarget(AKASHI_SHOP, None),
        FixedPatrolTarget(AKASHI_SHOP, (4, 4)),
    ]
    assert choose_best_target(targets).location == (4, 4)


# ---- 舰队选择 ----

def test_choose_fleet_for_target_picks_nearest():
    target = FixedPatrolTarget(AKASHI_SHOP, (5, 5))
    fleets = [
        FixedPatrolFleet(1, (5, 5)),   # distance 0
        FixedPatrolFleet(2, (5, 8)),   # distance 3
        FixedPatrolFleet(3, (0, 0)),   # distance 10
    ]
    assert choose_fleet_for_target(target, fleets).index == 1


def test_choose_fleet_for_target_skips_busy_and_unavailable():
    target = FixedPatrolTarget(AKASHI_SHOP, (5, 5))
    fleets = [
        FixedPatrolFleet(1, (5, 5), available=False),   # unavailable
        FixedPatrolFleet(2, (5, 5), busy=True),         # busy
        FixedPatrolFleet(3, (5, 8)),                    # nearest usable
        FixedPatrolFleet(4, (0, 0)),
    ]
    assert choose_fleet_for_target(target, fleets).index == 3


def test_choose_fleet_for_target_fallback_primary_when_unknown():
    target = FixedPatrolTarget(AKASHI_SHOP, (5, 5))
    fleets = [
        FixedPatrolFleet(2, None),
        FixedPatrolFleet(1, None),
    ]
    # 坐标未知时退回编号最小（主队优先）
    assert choose_fleet_for_target(target, fleets).index == 1


def test_choose_fleet_for_target_none_when_no_fleet_available():
    target = FixedPatrolTarget(AKASHI_SHOP, (5, 5))
    fleets = [FixedPatrolFleet(1, (5, 5), available=False)]
    assert choose_fleet_for_target(target, fleets) is None


# ---- 塞壬装置映射 ----

def make_grid(**kwargs):
    props = dict(
        is_scanning_device=False,
        is_logging_tower=False,
        is_akashi=False,
        is_exploration_reward=False,
        is_exploration_container=False,
        is_enemy=False,
        location=(0, 0),
    )
    props.update(kwargs)
    return SimpleNamespace(**props)


def test_build_targets_maps_siren_probe():
    grids = [make_grid(is_scanning_device=True, is_enemy=True, location=(2, 3))]
    targets = build_targets(grids)
    assert len(targets) == 1
    assert targets[0].kind == SIREN_PROBE
    assert targets[0].location == (2, 3)


def test_build_targets_maps_siren_information_device():
    grids = [make_grid(is_logging_tower=True, is_enemy=True, location=(4, 1))]
    targets = build_targets(grids)
    assert targets[0].kind == SIREN_INFORMATION_DEVICE


def test_build_targets_akashi_not_treated_as_normal_battle():
    # 明石在检测里也会带 is_enemy，必须先判具体类型，不能落到普通战斗
    grids = [make_grid(is_akashi=True, is_enemy=True, location=(1, 1))]
    targets = build_targets(grids)
    assert targets[0].kind == AKASHI_SHOP


def test_build_targets_maps_special_resource_and_battle():
    grids = [
        make_grid(is_exploration_reward=True, location=(1, 1)),
        make_grid(is_enemy=True, location=(5, 5)),
    ]
    kinds = {t.kind for t in build_targets(grids)}
    assert SPECIAL_RESOURCE in kinds
    assert NORMAL_BATTLE in kinds


def test_build_targets_skips_plain_sea_grids():
    grids = [make_grid()]
    assert build_targets(grids) == []


# ---- 防死循环 ----

def test_anti_loop_guards_no_progress():
    guard = AntiLoopGuard(max_repeats=3)
    assert not guard.check(SIREN_PROBE, (2, 3), 1, 'move')
    assert not guard.check(SIREN_PROBE, (2, 3), 1, 'move')
    assert not guard.check(SIREN_PROBE, (2, 3), 1, 'move')
    # 同一状态重复第 4 次触发
    assert guard.check(SIREN_PROBE, (2, 3), 1, 'move')


def test_anti_loop_resets_on_state_change():
    guard = AntiLoopGuard(max_repeats=3)
    for _ in range(3):
        assert not guard.check(SIREN_PROBE, (2, 3), 1, 'move')
    # 换了舰队，状态变了，重新计数
    assert not guard.check(SIREN_PROBE, (2, 3), 2, 'move')


def test_anti_loop_reset_method_clears_state():
    guard = AntiLoopGuard(max_repeats=3)
    for _ in range(4):
        guard.check(SIREN_PROBE, (2, 3), 1, 'move')
    guard.reset()
    assert guard.repeat_count == 0
    assert not guard.check(SIREN_PROBE, (2, 3), 1, 'move')
