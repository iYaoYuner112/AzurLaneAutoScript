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
        self.config = SimpleNamespace(
            OpsiHazard1Leveling_ExecuteFixedPatrolScan=value,
            cross_get=lambda keys, default=None: default)


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
        self._in_forced_recovery = False
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

    def _forced_move_enabled(self):
        return self.enabled

    def _is_meowfficer_task(self):
        return OSMap._is_meowfficer_task(self)

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

    def _move_fleets_and_rescan(self):
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


def test_no_zone_guard_matches_ap_master():
    """AP master 的入口没有海域守卫：别的海域照样按 AP 判断进入 L2（落点不存在时
    `_move_fleet_to_patrol` 会自行跳过，不会乱挪）。"""
    for hazard_level in (2, 5):
        stub, result = run_scan(hazard_level=hazard_level, current_ap=50)
        assert result is False
        assert stub.scan_calls == 1
        assert stub.move_calls == 1
    stub, result = run_scan(is_port=True, current_ap=50)
    assert result is False and stub.move_calls == 1


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


# ---- L0（换队读雷达）对齐 AP：有问号才清、清完立刻重扫捞事件 ----

class L0Stub:
    """只提供 `clear_question_any_fleet`（L0）需要的属性。"""

    def __init__(self, questions, reveal_on=None):
        self.config = SimpleNamespace(OpsiFleet_Fleet=1)
        self.zone = SimpleNamespace(is_port=False)
        self.questions = questions            # {fleet: 问号格子 or None}
        self.reveal_on = reveal_on            # 清问号后重扫命中的事件；None 表示没命中
        self.events = []
        self.current = None
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False
        self._question_unreachable = False
        self.device = SimpleNamespace(
            image=None, screenshot=lambda: self.events.append('screenshot'))
        self.radar = SimpleNamespace(
            predict_question=lambda image, in_port: self.questions.get(self.current))

    def _set_fixed_patrol_fleet(self, fleet):
        self.current = fleet
        self.events.append(f'check{fleet}')
        return True

    def fleet_set(self, fleet):
        self.current = fleet
        self.events.append(f'set{fleet}')

    def clear_question(self):
        self.events.append(f'clear{self.current}')

    def map_rescan_once(self, rescan_mode='full'):
        self.events.append('rescan')
        if self.reveal_on:
            self._solved_map_event = set(self.reveal_on)


def test_l0_does_not_run_clear_question_without_a_question():
    """雷达上没有问号就不走清除流程，也不重扫（对齐 AP 的先看雷达）。"""
    stub = L0Stub({})
    assert OSMap.clear_question_any_fleet(stub) is False
    assert not any(e.startswith('clear') for e in stub.events)
    assert 'rescan' not in stub.events
    # 4 支舰队都检查过，最后复位主队
    assert stub.events.count('screenshot') == 4
    assert stub.events[-1] == 'set1'


def test_l0_rescans_after_clearing_a_plain_question():
    """清掉普通问号后立刻整图重扫一次，把被遮挡的事件捞出来。"""
    stub = L0Stub({1: (4, 0), 2: None, 3: None, 4: None})
    assert OSMap.clear_question_any_fleet(stub) is False
    assert stub.events.count('clear1') == 1
    assert stub.events.count('rescan') == 1
    # 重扫只发生在清过问号之后
    assert stub.events.index('clear1') < stub.events.index('rescan')


def test_l0_rescan_hit_stops_before_moving_any_fleet():
    """重扫才显现的事件命中就停止，不再检查后续舰队（也就不会进 L2 挪舰队）。"""
    stub = L0Stub({1: (4, 0)}, reveal_on={'is_akashi'})
    assert OSMap.clear_question_any_fleet(stub) is True
    assert 'is_akashi' in stub._solved_map_event
    assert 'check2' not in stub.events
    assert 'check3' not in stub.events


# ---- 短猫绝不走共享 L2（对齐 AP master）----

def test_shared_fixed_patrol_skips_meowfficer():
    stub = ScanStub(enabled=True)
    stub.config.task = SimpleNamespace(command='OpsiMeowfficerFarming')
    result = OSMap.execute_fixed_patrol_scan(stub)
    assert result is False
    assert stub.move_calls == 0
    assert stub.scan_calls == 0
    assert stub.ap_reads == 0


def test_meowfficer_detected_directly_and_when_proxied():
    from module.os.tasks.task_context import OpsiTaskContext
    stub = ScanStub(enabled=True)
    stub.config.task = SimpleNamespace(command='OpsiMeowfficerFarming')
    assert OSMap._is_meowfficer_task(stub) is True

    stub.config.task = SimpleNamespace(command='OpsiScheduling')
    assert OSMap._is_meowfficer_task(stub) is False

    # 智能调度代理短猫时，身份在 task context 里，config.task 是子任务自己
    stub.config._opsi_context = OpsiTaskContext(
        parent_task='OpsiScheduling', current_task='OpsiMeowfficerFarming')
    assert OSMap._is_meowfficer_task(stub) is True


def test_meowfficer_guard_missing_config_does_not_crash():
    """老配置/替身没有 task 属性时也不能炸（保持向后兼容）。"""
    stub = ScanStub(enabled=True)
    assert OSMap._is_meowfficer_task(stub) is False


# ---- 事件不可达兜底（对齐 AP master：换其他舰队 → 仍不行则强制移动）----

class RecoveryStub:
    def __init__(self, rotation_succeeds=False, meow=False):
        self.config = SimpleNamespace(
            temporary=lambda **kwargs: SimpleNamespace(recover=lambda: None))
        self._unreachable_event_nodes = set()
        self._rotation_succeeds = rotation_succeeds
        self._meow = meow
        self.patrol_calls = 0

    def _is_meowfficer_task(self):
        return self._meow

    def _goto_akashi_with_other_fleets(self, drop=None):
        return self._rotation_succeeds

    def execute_fixed_patrol_scan(self):
        self.patrol_calls += 1

    _mark_event_unreachable = OSMap._mark_event_unreachable
    _recover_unreachable_akashi = OSMap._recover_unreachable_akashi


def test_unreachable_akashi_rotation_succeeds_without_patrol():
    stub = RecoveryStub(rotation_succeeds=True)
    assert OSMap._recover_unreachable_akashi(stub, None, 'B7') is True
    assert stub.patrol_calls == 0


def test_unreachable_akashi_falls_back_to_patrol_once_per_round():
    stub = RecoveryStub(rotation_succeeds=False)
    assert OSMap._recover_unreachable_akashi(stub, None, 'B7') is False
    assert stub.patrol_calls == 1
    assert 'B7' in stub._unreachable_event_nodes
    # 同一格在本轮重扫里不再重复触发大规模挪舰队
    assert OSMap._recover_unreachable_akashi(stub, None, 'B7') is False
    assert stub.patrol_calls == 1


def test_unreachable_akashi_fallback_skipped_for_meowfficer():
    stub = RecoveryStub(rotation_succeeds=False, meow=True)
    assert OSMap._recover_unreachable_akashi(stub, None, 'B7') is False
    assert stub.patrol_calls == 0


def test_mark_event_unreachable_rebinds_instead_of_leaking():
    a = RecoveryStub()
    b = RecoveryStub()
    a._mark_event_unreachable('B7')
    assert a._unreachable_event_nodes == {'B7'}
    assert b._unreachable_event_nodes == set()


def test_radar_question_to_local_returns_none_without_a_question():
    stub = SimpleNamespace(
        device=SimpleNamespace(image=None), zone=SimpleNamespace(is_port=False),
        radar=SimpleNamespace(predict_question=lambda image, in_port: None))
    assert OSMap._radar_question_to_local(stub) is None


def test_fallback_patrol_not_blocked_by_other_solved_events():
    """_solved_map_event 的门槛在调用方，入口本身不再拦（AP master 同款）。"""
    stub = ScanStub(enabled=True, current_ap=50)
    stub._solved_map_event = {'is_logging_tower'}
    assert OSMap.execute_fixed_patrol_scan(stub) is False
    assert stub.move_calls == 1


# ---- 装置不可达：换其他舰队点（对齐 AP master 的效果）----

class _TempContext:
    """`config.temporary()` 的最小替身：既能 with，也能 recover。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recover(self):
        pass


class DeviceRotationStub:
    def __init__(self, reachable_fleet=None):
        self.config = SimpleNamespace(temporary=lambda **kwargs: _TempContext())
        self._reachable = reachable_fleet
        self._current = 1
        self.fleet_events = []
        self.device_states = []
        self.walk_calls = 0
        self.device = SimpleNamespace(image=None, screenshot=lambda: None,
                                      click=lambda grid: None)
        self.view = SimpleNamespace(
            predict=lambda: None,
            select=lambda **kwargs: [SimpleNamespace(is_scanning_device=True)])

    @property
    def fleet_selector(self):
        outer = self
        return SimpleNamespace(get=lambda: outer._current)

    def fleet_set(self, fleet):
        self._current = fleet
        self.fleet_events.append(fleet)
        return True

    def update_os(self):
        pass

    def wait_until_walk_stable(self, **kwargs):
        self.walk_calls += 1
        return 'event' if self._current == self._reachable else 'timeout'

    def _set_device_state(self, state):
        self.device_states.append(state)

    _goto_scanning_device_with_other_fleets = OSMap._goto_scanning_device_with_other_fleets


def test_device_rotation_reaches_with_another_fleet():
    stub = DeviceRotationStub(reachable_fleet=3)
    assert OSMap._goto_scanning_device_with_other_fleets(stub, None) is True
    assert stub.device_states == ['DEVICE_DIALOG_OPEN']
    # 结束时恢复原舰队
    assert stub.fleet_events[-1] == 1


def test_device_rotation_fails_and_restores_original_fleet():
    stub = DeviceRotationStub(reachable_fleet=None)
    assert OSMap._goto_scanning_device_with_other_fleets(stub, None) is False
    assert stub.fleet_events[-1] == 1
    assert stub.walk_calls == 3      # 除了原舰队，另外三队都试过


# ---- clear_question 的 _question_unreachable 语义（对齐 AP master）----
#
# 用户实测场景：问号点过去开的是普通剧情事件（3 选项 + 奖励），wait 返回 'event'。
# 旧实现把它标成 question_unreachable → L2 无视行动力去挪舰队追一个已经消失的问号。
# master 的语义：只有 3 次尝试都看到问号却没清掉才置位。

class _FakeGrid:
    def __init__(self):
        self.str = 'B4'
        self.is_logging_tower = False
        self.is_scanning_device = False


class ClearQuestionStub:
    def __init__(self, predictions, walk_results=None, convert_error=False, fleet_visible=True,
                 confirmed_on_walk=False, siren_device_mode=None):
        self.config = SimpleNamespace(
            temporary=lambda **kwargs: _TempContext(),
            task=SimpleNamespace(command='OpsiHazard1Leveling'),
            cross_get=lambda keys, default=None: default)
        self.zone = SimpleNamespace(is_port=False)
        self._solved_map_event = set()
        self._question_unreachable = False
        self.events = []
        self.is_siren_device_confirmed = False
        self.siren_device_mode = siren_device_mode
        self.auto_search_calls = 0
        self.fleet_sets = []
        self._confirmed_on_walk = confirmed_on_walk
        self._predictions = list(predictions or [])
        self._walk_results = list(walk_results or [])
        self._convert_error = convert_error
        self._fleet_visible = fleet_visible
        self.device = SimpleNamespace(image=None, screenshot=lambda: None,
                                      click=lambda grid: self.events.append('click'))
        self.radar = SimpleNamespace(
            predict_question=lambda image, in_port: (
                self._predictions.pop(0) if self._predictions else None))
        self.view = SimpleNamespace(
            predict=lambda: None,
            show=lambda: None,
            select=lambda **kwargs: SimpleNamespace(
                count=1 if self._fleet_visible else 0))
        self.fleet_selector = SimpleNamespace(get=lambda: 2)

    def handle_info_bar(self):
        pass

    def update_os(self):
        pass

    def convert_radar_to_local(self, grid):
        if self._convert_error:
            raise KeyError('out of view')
        return _FakeGrid()

    def wait_until_walk_stable(self, **kwargs):
        # 模拟真实 story_skip：识别到装置剧情会置位 is_siren_device_confirmed
        if self._confirmed_on_walk:
            self.is_siren_device_confirmed = True
        return self._walk_results.pop(0) if self._walk_results else 'timeout'

    def os_auto_search_run(self, drop=None):
        self.auto_search_calls += 1

    def fleet_set(self, fleet):
        self.fleet_sets.append(fleet)
        return True

    def _os_camera_recover_to_fleet(self, fleet=None):
        self.events.append('recover')
        return True

    clear_question = OSMap.clear_question


def test_story_event_from_question_is_not_marked_unreachable():
    """问号开成普通剧情并消费掉：不能标 unreachable，否则 L2 会去追已消失的问号。"""
    stub = ClearQuestionStub(predictions=[(0, -1), None], walk_results=['event'])
    assert OSMap.clear_question(stub) is False
    assert stub._question_unreachable is False
    assert stub.events == ['click']


def test_all_attempts_failed_marks_unreachable():
    """3 次都看到问号却没清掉（相邻双舰队机关）：置位，交给 L2。"""
    stub = ClearQuestionStub(
        predictions=[(0, -1), (0, -1), (0, -1), (0, -1)],
        walk_results=['timeout', 'timeout', 'timeout'])
    assert OSMap.clear_question(stub) is False
    assert stub._question_unreachable is True


def test_out_of_view_question_recovers_camera_without_unreachable():
    stub = ClearQuestionStub(predictions=[(0, -1), None], convert_error=True,
                             fleet_visible=False)
    assert OSMap.clear_question(stub) is False
    assert stub._question_unreachable is False
    assert 'recover' in stub.events


def test_no_question_on_radar_returns_false_cleanly():
    stub = ClearQuestionStub(predictions=[None])
    assert OSMap.clear_question(stub) is False
    assert stub._question_unreachable is False
    assert stub.events == []


def test_story_device_confirmed_solves_and_stops_patrol():
    """问号点过去开出信息收集装置（剧情已点完）：视为已解决，巡逻停止且不自律。"""
    stub = ClearQuestionStub(
        predictions=[(0, -1), None], walk_results=['event'],
        confirmed_on_walk=True, siren_device_mode='collected')
    assert OSMap.clear_question(stub) is True
    assert 'is_scanning_device' in stub._solved_map_event
    assert stub.auto_search_calls == 0      # collected 模式无需自律寻敌
    assert stub._question_unreachable is False


def test_story_device_unknown_mode_runs_auto_search_once():
    stub = ClearQuestionStub(
        predictions=[(0, -1), None], walk_results=['event'],
        confirmed_on_walk=True, siren_device_mode=None)
    assert OSMap.clear_question(stub) is True
    assert stub.auto_search_calls == 1
    assert 'is_scanning_device' in stub._solved_map_event
