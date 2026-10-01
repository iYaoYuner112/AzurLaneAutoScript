"""强制移动（侵蚀一）的边界锁定。

对齐 AzurPilot 的强制移动：L0/L1 只换队读雷达、一支舰队都不挪；L2 才逐队挪动
舰队并整图重扫。要不要挪分两种——雷达上看到问号却到不了的必须挪（与行动力无关），
全队雷达都没线索时才看当前行动力（大于 7 才挪，否则留给下一轮练级）。

这里锁定开关读取、编排分支和候选落点的生成规则。
"""

from types import SimpleNamespace

from module.os.fixed_patrol import AntiLoopGuard
from module.os.map import ALREADY_SOLVED_MAP_EVENTS, OSMap


class SwitchStub:
    """只提供 `_forced_move_enabled` 需要的属性。"""

    def __init__(self, value):
        self.config = SimpleNamespace(OpsiScheduling_ExecuteFixedPatrolScan=value)


def enabled(value):
    return OSMap._forced_move_enabled(SwitchStub(value))


def test_switch_reads_bool():
    """新配置是复选框，直接读布尔值。"""
    assert enabled(True) is True
    assert enabled(False) is False


def test_switch_reads_string_values():
    """配置文件里读出来的字符串也要认。"""
    for value in ('true', 'TRUE', '1', '2', '3'):
        assert enabled(value) is True, value
    for value in ('false', 'False', '0', ''):
        assert enabled(value) is False, value


def test_switch_accepts_legacy_levels():
    """旧版等级配置：0=关闭，1/2/3 都视为开启。"""
    assert enabled(0) is False
    for level in (1, 2, 3):
        assert enabled(level) is True, level
    assert enabled(None) is False


class ScanStub:
    """只提供 `execute_fixed_patrol_scan` 需要的属性。"""

    # 阈值取真实实现，避免测试和代码各写一份
    _FIXED_PATROL_L2_AP = OSMap._FIXED_PATROL_L2_AP

    def __init__(self, enabled=True, radar_solved=False, current_ap=0,
                 unreachable=False, has_grids=True, hazard_level=1, is_port=False):
        self.config = SimpleNamespace(
            OpsiFleet_Fleet=1,
            temporary=lambda **kwargs: SimpleNamespace(recover=lambda: None),
        )
        self.zone = SimpleNamespace(is_port=is_port, hazard_level=hazard_level)
        self.map = SimpleNamespace(grids=[object()] if has_grids else [])
        self._fixed_patrol_loop_guard = AntiLoopGuard(max_repeats=3)
        self.enabled = enabled
        self.radar_solved = radar_solved
        self.current_ap = current_ap
        self.unreachable = unreachable
        self.ap_reads = 0
        self.move_calls = 0
        self.scan_calls = 0
        self.fleet_sets = []
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False
        self._question_unreachable = False

    def map_init(self, map_=None):
        pass

    def _fixed_patrol_targets(self):
        # 测试里不读真实地图，直接返回空目标，走「无已知目标」分支。
        return []

    def _fixed_patrol_fleets(self):
        # 测试里不记录舰队位置，返回空列表 -> choose_fleet_for_target 返回 None。
        return []

    def _forced_move_enabled(self):
        return self.enabled

    def clear_question_any_fleet(self):
        # 真实实现会在清不掉时置位 `_question_unreachable`，这里直接给定
        self.scan_calls += 1
        self._question_unreachable = self.unreachable
        if self.radar_solved:
            self._solved_map_event.add('is_akashi')
            return True
        return False

    def _read_current_action_point(self):
        self.ap_reads += 1
        return self.current_ap

    def _move_fleets_and_rescan(self, best_target=None, fleets=None):
        self.move_calls += 1

    def fleet_set(self, index=1):
        self.fleet_sets.append(index)
        return True


def run_scan(**kwargs):
    stub = ScanStub(**kwargs)
    result = OSMap.execute_fixed_patrol_scan(stub)
    return stub, result


def test_switch_off_does_nothing():
    """开关关闭 -> 什么都不做，连行动力都不去查。"""
    stub, result = run_scan(enabled=False)
    assert result is False
    assert stub.scan_calls == 0
    assert stub.ap_reads == 0
    assert stub.move_calls == 0
    assert stub.fleet_sets == []


def test_solved_during_zero_move_scan_skips_l2():
    """零移动检索就找到了事件 -> 直接结束，不查行动力也不挪舰队。"""
    stub, result = run_scan(radar_solved=True)
    assert result is True
    assert stub.scan_calls == 1
    assert stub.ap_reads == 0
    assert stub.move_calls == 0
    assert stub.fleet_sets == [1]


def test_skips_when_no_map_grids():
    """地图数据没读出来 -> 跳过，别在空地图上乱点。"""
    stub, result = run_scan(has_grids=False)
    assert result is False
    assert stub.scan_calls == 0
    assert stub.move_calls == 0
    assert stub.fleet_sets == []


def test_skips_on_non_hazard_one_map():
    """落点是照侵蚀1 那张图定的，别的海域不跑这套。"""
    for hazard_level in (2, 5):
        stub, result = run_scan(hazard_level=hazard_level)
        assert result is False
        assert stub.scan_calls == 0
        assert stub.move_calls == 0
    stub, result = run_scan(is_port=True)
    assert result is False and stub.move_calls == 0


def test_moves_fleets_when_action_point_enough():
    """什么都没找到但当前行动力大于 7 -> 走一遍 L2 挪舰队。"""
    stub, _ = run_scan(current_ap=8)
    assert stub.ap_reads == 1
    assert stub.move_calls == 1


def test_keeps_farming_when_action_point_low():
    """当前行动力不够 -> 不挪舰队，留给下一轮正常练级。"""
    for current_ap in (0, 5, 7):
        stub, _ = run_scan(current_ap=current_ap)
        assert stub.ap_reads == 1, current_ap
        assert stub.move_calls == 0, current_ap


def test_seen_but_unreachable_moves_regardless_of_action_point():
    """看到问号却点不到 -> 不看行动力，直接挪舰队：已看见的事件不能放跑。"""
    for current_ap in (0, 3, 7):
        stub, _ = run_scan(current_ap=current_ap, unreachable=True)
        assert stub.ap_reads == 0, current_ap
        assert stub.move_calls == 1, current_ap


def test_action_point_threshold_is_seven():
    """阈值就是 7：大于 7 才挪，等于 7 不挪。"""
    assert OSMap._FIXED_PATROL_L2_AP == 7
    stub, _ = run_scan(current_ap=7)
    assert stub.move_calls == 0
    stub, _ = run_scan(current_ap=8)
    assert stub.move_calls == 1


def test_primary_fleet_restored_after_scan():
    """无论走哪条分支，结束后都要复位主队。"""
    for kwargs in (dict(radar_solved=True), dict(current_ap=30), dict(current_ap=0)):
        stub, _ = run_scan(**kwargs)
        assert stub.fleet_sets == [1], kwargs


def make_grid(location, **flags):
    attrs = dict(
        is_land=False, is_enemy=False, is_siren=False, is_boss=False,
        is_fortress=False, is_mechanism_block=False, is_fleet=False,
    )
    attrs.update(flags)
    return SimpleNamespace(location=location, **attrs)


class MapStub:
    """只提供 `_fixed_patrol_candidate_grids` 需要的映射行为。"""

    def __init__(self, grids):
        self._grids = {grid.location: grid for grid in grids}

    def __contains__(self, location):
        return location in self._grids

    def __getitem__(self, location):
        return self._grids[location]


class CandidateStub:
    """只提供 `_fixed_patrol_candidate_grids` 需要的属性。"""

    def __init__(self, grids):
        self.map = MapStub(grids)


def candidate_locations(grids, target_loc, occupied=None):
    stub = CandidateStub(grids)
    grids_ = OSMap._fixed_patrol_candidate_grids(stub, target_loc, occupied)
    return [grid.location for grid in grids_]


def test_candidates_start_from_target_and_skip_blocked_grids():
    """目标格排第一，陆地/敌人/塞壬/要塞/舰队占着的格子都要跳过。"""
    grids = [
        make_grid((2, 0)),
        make_grid((2, 1), is_land=True),
        make_grid((2, 2)),
        make_grid((1, 1)),
        make_grid((3, 1), is_fleet=True),
        make_grid((1, 2), is_enemy=True),
        make_grid((3, 2), is_siren=True),
        make_grid((1, 0)),
        make_grid((3, 0), is_fortress=True),
        make_grid((2, 3), is_mechanism_block=True),
        make_grid((2, 11)),
        make_grid((2, 12)),
    ]
    assert candidate_locations(grids, (2, 0)) == [(2, 0), (2, 2), (1, 1), (1, 0), (2, 11), (2, 12)]


def test_candidates_skip_missing_and_occupied_grids():
    """地图上没有的格子、以及已给占位清单里的格子都要跳过。"""
    grids = [
        make_grid((2, 0)),
        make_grid((2, 2)),
        make_grid((1, 1)),
        make_grid((2, 11)),
        make_grid((2, 12)),
    ]
    assert candidate_locations(grids, (2, 0)) == [(2, 0), (2, 2), (1, 1), (2, 11), (2, 12)]
    assert candidate_locations(grids, (2, 0), {(2, 0), (2, 2)}) == [(1, 1), (2, 11), (2, 12)]


def test_candidates_fall_back_to_far_rows_when_nothing_else():
    """目标附近全被占 -> 退到几行之外的同列落点，别返回空。"""
    grids = [
        make_grid((2, 0), is_land=True),
        make_grid((2, 1), is_land=True),
        make_grid((2, 2), is_land=True),
        make_grid((1, 1), is_land=True),
        make_grid((3, 1), is_land=True),
        make_grid((1, 2), is_land=True),
        make_grid((3, 2), is_land=True),
        make_grid((1, 0), is_land=True),
        make_grid((3, 0), is_land=True),
        make_grid((2, 3), is_land=True),
        make_grid((2, 11)),
        make_grid((2, 12)),
    ]
    assert candidate_locations(grids, (2, 0)) == [(2, 11), (2, 12)]


def test_solved_events_constant_is_used():
    """编排里判定“事件已解决”用的是共享常量，别写死字符串。"""
    assert 'is_akashi' in ALREADY_SOLVED_MAP_EVENTS
