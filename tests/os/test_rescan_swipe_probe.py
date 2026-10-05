"""大世界重扫的两处改动：爬角发散保护 + 滑动后事件探测。

计划作战停下来之后要做整图重扫：先爬到地图角落标定镜头，再按镜头格子一站一站站定才判断
有没有事件。旧代码有两个洞——

1. 滑动过程只判断"到没到边"，从不判断"画面有没有真的滚"。游戏到边就不再滚图，而边界又是
   暗的时候边缘检测报不出来，相机坐标却每轮照滑动量累加，于是一路白滑（OS 里
   `MAP_SWIPE_PREDICT=False`，没有主图那种反推真实滑动量的兜底）。现在连续两帧画面不动才
   停轴并退回虚增坐标；单帧不算，因为锚点是按一格取模的相位，正好滚完整数格时位移也是 0。
   另有滑动次数硬上限。
2. 滑动过程看见了事件也不理，要等镜头走到规划好的格子才点（2026-10-05 的日志里塞壬信息塔
   第一次滑动就进画面，4.2 秒后才点上）。现在每次滑动后顺手看一眼，看见就立刻处理并结束
   重扫；贴屏幕边的格子交给原来的站定流程，免得点到底部按钮排。
"""

import numpy as np
from types import SimpleNamespace

from module.config.config import TaskEnd
from module.exception import MapDetectionError
from module.map.camera import Camera
from module.os.map import OSMap

REAL = (3.0, 2.0)
NO_EDGE = (False, False, False, False)
ALL_EDGE = (True, True, True, True)


def frame(edges=NO_EDGE, move=REAL):
    """一帧剧本：edges=(左, 右, 上, 下) 有没有被检测到，move=这一滑画面真实滚了多少格。"""
    return {'edges': edges, 'move': move}


class PanStub:
    """`Camera.ensure_edge_insight()` / `Camera.focus_to()` 需要的最小对象。

    `map_swipe()` 按剧本改写边缘与同位锚点，并像真实现那样把滑动量累加进相机坐标。
    """

    SWIPE_STALL_RATIO = Camera.SWIPE_STALL_RATIO
    SWIPE_STALL_FRAMES = Camera.SWIPE_STALL_FRAMES
    EDGE_INSIGHT_SWIPE_LIMIT = Camera.EDGE_INSIGHT_SWIPE_LIMIT
    # 借用真实现的两个小工具，免得测试和代码各写一份判据
    _swipe_moved_grids = Camera._swipe_moved_grids
    _swipe_stall_check = Camera._swipe_stall_check
    _probe_after_swipe = Camera._probe_after_swipe

    def __init__(self, frames=(), corner='bottom-right', inplace=False, probe_after=None):
        self.config = SimpleNamespace(MAP_ENSURE_EDGE_INSIGHT_CORNER=corner)
        self.backend = SimpleNamespace(homo_loca=np.array([50.0, 50.0]))
        self.view = SimpleNamespace(
            backend=self.backend,
            swipe_base=np.array([10.0, 10.0]),
            left_edge=False, right_edge=False, upper_edge=False, lower_edge=False,
        )
        self.frames = list(frames)
        # inplace=True 时原地改锚点数组，复现 `homo_loca %= HOMO_TILE` 那种写法
        self.inplace = inplace
        self.camera = (3, 3)
        self.gestures = []
        self.probe_calls = 0
        # None 表示没装钩子；整数表示第几次调用起返回 True
        self.probe_after = probe_after
        self._swipe_probe = None if probe_after is None else self._probe

    def _probe(self):
        self.probe_calls += 1
        return self.probe_calls >= self.probe_after

    def _spec(self):
        if not self.frames:
            return frame()
        return self.frames[min(len(self.gestures) - 1, len(self.frames) - 1)]

    def map_swipe(self, vector):
        vector = tuple(int(v) for v in vector)
        if vector == (0, 0):
            # 真实现里这一滑会被丢掉：不截图、不滚图、也不动相机
            return False
        self.gestures.append(vector)
        spec = self._spec()
        left, right, upper, lower = spec['edges']
        self.view.left_edge, self.view.right_edge = left, right
        self.view.upper_edge, self.view.lower_edge = upper, lower
        if spec['move'] is None:
            self.view.backend.homo_loca = None
        else:
            delta = np.array(spec['move'], dtype=float) * self.view.swipe_base
            if self.view.backend.homo_loca is None:
                self.view.backend.homo_loca = np.array([50.0, 50.0])
            if self.inplace:
                self.view.backend.homo_loca += delta
            else:
                self.view.backend.homo_loca = self.view.backend.homo_loca + delta
        # 没有边缘可参照时相机就是纯推算，跟真实现一样按滑动量累加
        self.camera = (self.camera[0] + vector[0], self.camera[1] + vector[1])
        return True


def run_pan(stub, **kwargs):
    return Camera.ensure_edge_insight(stub, **kwargs)


def test_normal_pan_is_untouched():
    """画面每帧都在滚、边缘正常报出来：一帧都不许多滑，也不退回坐标。"""
    stub = PanStub([frame(), frame(edges=(False, True, True, False))])
    record = run_pan(stub)
    assert stub.gestures == [(3, 2), (3, 2)], stub.gestures
    assert stub.camera == (9, 7), stub.camera
    assert record[-1] == (0, 0)


def test_stalled_axis_stops_and_gives_phantom_camera_back():
    """连续两帧画面不动 → 停掉这条轴，并把两帧虚增的相机坐标退回。"""
    stub = PanStub([frame(), frame(move=(0.0, 0.0)), frame(move=(0.0, 0.0))])
    run_pan(stub)
    # 只有第一滑真的滚了图，后两滑是游戏到边不再滚、而检测又没报出边缘
    assert stub.gestures == [(3, 2)] * 3, stub.gestures
    assert stub.camera == (6, 5), stub.camera


def test_single_stalled_frame_is_not_enough():
    """单帧位移为 0 不能算到头：正好滚完整数格时锚点位移也是 0。"""
    stub = PanStub([
        frame(), frame(move=(0.0, 0.0)), frame(), frame(move=(0.0, 0.0)),
        frame(edges=(True, False, True, False)),
    ])
    run_pan(stub)
    # 两帧 0 位移不相邻，所以一次都不该停轴、一次都不该退坐标
    assert stub.gestures == [(3, 2)] * 5, stub.gestures
    assert stub.camera == (18, 13), stub.camera


def test_sub_tile_residual_is_not_mistaken_for_a_stall():
    """实测合法滑动的锚点残差只有约 0.1 格（9~19 像素），不能被判成"画面没滚"。

    判据设在半格的话，正常爬角两三跳就会被误判到头，还会把真实移动从相机坐标里退掉，
    所以这里锁一个"合法但很小"的位移必须照常继续滑、一帧都不许退回坐标。
    """
    stub = PanStub([
        frame(move=(0.11, 0.13)), frame(move=(0.11, 0.13)), frame(move=(0.11, 0.13)),
        frame(edges=ALL_EDGE, move=(0.11, 0.13)),
    ])
    run_pan(stub)
    # 四跳都被认成"真的滚了"：既没提前停轴，也没退回任何相机坐标
    # （旧阈值半格时第二跳就会停轴并把坐标减掉）
    assert stub.gestures == [(3, 2)] * 4, stub.gestures
    assert stub.camera == (15, 11), stub.camera


def test_in_place_anchor_update_does_not_fool_the_guard():
    """锚点数组被原地改写时也不能误判：滑动前必须先拷一份。"""
    stub = PanStub([
        frame(), frame(), frame(edges=(False, True, False, True)),
    ], inplace=True)
    run_pan(stub)
    assert stub.gestures == [(3, 2)] * 3, stub.gestures
    assert stub.camera == (12, 9), stub.camera


def test_unreadable_anchor_does_not_stop_the_pan():
    """锚点读不到（None）时按"画面正常"处理，不能停轴也不能退坐标。"""
    stub = PanStub([frame(move=None), frame(move=None), frame(edges=ALL_EDGE)])
    run_pan(stub)
    assert stub.gestures == [(3, 2)] * 3, stub.gestures
    assert stub.camera == (12, 9), stub.camera


def test_pan_has_a_hard_swipe_cap():
    """画面一直在滚但边缘永远报不出来：靠次数上限退出，不能无限滑。"""
    stub = PanStub([frame() for _ in range(30)])
    record = run_pan(stub)
    assert len(stub.gestures) <= Camera.EDGE_INSIGHT_SWIPE_LIMIT + 1, stub.gestures
    assert len(record) <= Camera.EDGE_INSIGHT_SWIPE_LIMIT + 2, record


def test_probe_stops_the_corner_pan():
    """滑动后被钩子接手（真处理了事件）就该立刻停止爬角，不再白滑。"""
    stub = PanStub([frame(), frame(), frame()], probe_after=1)
    run_pan(stub, preset=(0, -5))
    assert stub.gestures == [(0, -5), (3, 2)], stub.gestures
    assert stub.probe_calls == 1, stub.probe_calls


def test_probe_stops_focus_to_mid_approach():
    """镜头往规划格子走的途中看见事件也要停手，不用先走完最后一跳。"""
    stub = PanStub(probe_after=2)
    stub.camera = (0, 0)
    Camera.focus_to(stub, (6, 6), swipe_limit=(2, 2))
    # 到不了 (6,6)，但第二次滑动后钩子返回 True
    assert stub.gestures == [(2, 2), (2, 2)], stub.gestures
    assert stub.probe_calls == 2, stub.probe_calls


def test_no_probe_means_no_behaviour_change():
    """没装钩子时滑动循环的行为一个字节都不变（主图攻略走的就是这条）。"""
    stub = PanStub([frame(), frame(edges=(True, False, False, True))])
    assert stub._swipe_probe is None
    run_pan(stub)
    assert stub.probe_calls == 0
    assert stub.gestures == [(3, 2), (3, 2)], stub.gestures


class EventGrid:
    def __init__(self, button=(300, 200, 360, 260), **flags):
        self.button = button
        self.location = (3, 4)
        for flag in OSMap.PROBE_EVENT_FLAGS:
            setattr(self, flag, False)
        for key, value in flags.items():
            setattr(self, key, value)

    def __str__(self):
        return 'D5'


class ViewStub:
    def __init__(self, hits=None):
        self.hits = hits or {}
        self.select_calls = []

    def select(self, **kwargs):
        self.select_calls.append(tuple(kwargs))
        return self.hits.get(tuple(kwargs), [])


class ProbeStub:
    """只提供 `_probe_events_after_swipe()` / `_probe_grid_clickable()` 需要的东西。"""

    PROBE_CLICK_SAFE_MARGIN = OSMap.PROBE_CLICK_SAFE_MARGIN
    PROBE_EVENT_FLAGS = OSMap.PROBE_EVENT_FLAGS
    _probe_grid_clickable = OSMap._probe_grid_clickable

    def __init__(self, hits=None, solved=(), handler_result=True, handler_error=None,
                 image=(720, 1280, 3), active=True, busy=False):
        self.view = ViewStub(hits)
        self.device = SimpleNamespace(image=None if image is None else np.zeros(image))
        self._solved_map_event = set(solved)
        self._rescan_probe_on = active
        self._rescan_probe_busy = busy
        self._rescan_probe_solved = False
        self._rescan_probe_drop = 'drop'
        self.handler_calls = 0
        self.handler_drops = []
        self._result = handler_result
        self._error = handler_error

    def map_rescan_current(self, drop=None):
        self.handler_calls += 1
        self.handler_drops.append(drop)
        if self._error is not None:
            raise self._error
        return self._result

    def probe(self):
        return OSMap._probe_events_after_swipe(self)


def tower(button=(300, 200, 360, 260)):
    return {('is_logging_tower',): [EventGrid(button=button, is_logging_tower=True)]}


def checked(calls):
    return [tuple(k) for k in calls]


def test_probe_handles_event_seen_mid_pan():
    stub = ProbeStub(hits=tower())
    assert stub.probe() is True
    assert stub.handler_calls == 1
    assert stub._rescan_probe_solved is True
    # drop 要传下去，掉落记录不能断
    assert stub.handler_drops == ['drop']
    assert stub._rescan_probe_busy is False


def test_probe_leaves_edge_hugging_grid_to_the_walk():
    """贴屏幕边（左侧舰队条、底部按钮排）的格子不由探测来点。"""
    stub = ProbeStub(hits=tower(button=(20, 600, 90, 700)))
    assert stub.probe() is False
    assert stub.handler_calls == 0


def test_probe_ignores_already_solved_event():
    """同一轮已经处理过的类型不再重复点。"""
    stub = ProbeStub(hits=tower(), solved=('is_logging_tower',))
    assert stub.probe() is False
    assert checked(stub.view.select_calls) == [('is_exploration_container',),
                                               ('is_exploration_reward',),
                                               ('is_akashi',),
                                               ('is_scanning_device',)]
    assert stub.handler_calls == 0


def test_probe_follows_rescan_branch_order():
    """判定顺序跟 map_rescan_current 一致：明石贴边就不点，哪怕装置在中间。"""
    stub = ProbeStub(hits={
        ('is_akashi',): [EventGrid(button=(20, 20, 80, 80), is_akashi=True)],
        ('is_scanning_device',): [EventGrid(is_scanning_device=True)],
    })
    assert stub.probe() is False
    assert stub.handler_calls == 0
    assert checked(stub.view.select_calls) == [('is_exploration_container',),
                                               ('is_exploration_reward',),
                                               ('is_akashi',)]


def test_probe_is_off_outside_a_rescan():
    stub = ProbeStub(hits=tower(), active=False)
    assert stub.probe() is False
    assert stub.view.select_calls == []


def test_probe_is_off_while_it_is_already_running():
    """处理事件时自己也会滑镜头（换队点、强制移动），不能递归再进探测。"""
    stub = ProbeStub(hits=tower(), busy=True)
    assert stub.probe() is False
    assert stub.view.select_calls == []


def test_probe_wants_nothing_when_view_has_no_event():
    stub = ProbeStub(hits=None)
    assert stub.probe() is False
    assert stub.handler_calls == 0
    assert len(stub.view.select_calls) == len(OSMap.PROBE_EVENT_FLAGS)


def test_probe_passes_task_switching_through():
    """TaskEnd 不是 ScriptEnd 的子类，被探测吞掉就会原地打转，必须原样抛出。"""
    stub = ProbeStub(hits=tower(), handler_error=TaskEnd('switch task'))
    raised = False
    try:
        stub.probe()
    except TaskEnd:
        raised = True
    assert raised
    # 抛出去之后锁要放开，不然下一帧起就再也不探测了
    assert stub._rescan_probe_busy is False


def test_probe_swallows_only_unexpected_errors():
    stub = ProbeStub(hits=tower(), handler_error=ValueError('bad frame'))
    assert stub.probe() is False
    assert stub._rescan_probe_busy is False
    assert stub._rescan_probe_solved is False


def test_probe_waits_when_there_is_no_screenshot():
    """截图还没落地时不点，等下一帧。"""
    stub = ProbeStub(hits=tower(), image=None)
    assert stub.probe() is False
    assert stub.handler_calls == 0


class Queue(list):
    """镜头格子队列：只需要 len / 下标 / 切片 / 按距离排序。"""

    def sort_by_camera_distance(self, camera):
        return self

    def __getitem__(self, item):
        if isinstance(item, slice):
            return Queue(list.__getitem__(self, item))
        return list.__getitem__(self, item)


LATTICE = [(4, 1), (4, 6), (4, 9)]


class RescanStub:
    """`map_rescan_once()` 需要的对象：钩子的装卸和两个提前结束点。"""

    def __init__(self, lattice=None, solved_in_pan=False, solved_at_focus=0,
                 current_hit=False, map_error=None):
        self.map = SimpleNamespace(camera_data=Queue(list(LATTICE if lattice is None else lattice)))
        self.camera = (3, 3)
        self._unreachable_event_nodes = set()
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False
        self._rescan_probe_on = False
        self._rescan_probe_busy = False
        self._rescan_probe_drop = None
        self._rescan_probe_solved = False
        self._swipe_probe = None
        self._solved_in_pan = solved_in_pan
        self._solved_at_focus = solved_at_focus
        self._current_hit = current_hit
        self._map_error = map_error
        self.focuses = []
        self.centers = 0
        self.rescans = 0
        self.probe_seen_in_walk = []

    def map_data_init(self, map_=None):
        pass

    def handle_info_bar(self):
        pass

    def update(self, *args, **kwargs):
        pass

    def _probe_events_after_swipe(self):
        # 探测本身的判定由 ProbeStub 的用例覆盖，这里只模拟"它处理掉了事件"
        return False

    def map_init(self, map_=None):
        # 爬角期间钩子必须在位，这就是这次改动的开关点
        assert self._rescan_probe_on is True
        assert self._swipe_probe is not None
        if self._map_error is not None:
            raise self._map_error
        if self._solved_in_pan:
            self._rescan_probe_solved = True

    def focus_to(self, location, swipe_limit=(4, 3)):
        self.focuses.append(tuple(location))
        if self._solved_in_pan or (self._solved_at_focus and len(self.focuses) >= self._solved_at_focus):
            self._rescan_probe_solved = True

    def focus_to_grid_center(self, tolerance=None):
        self.centers += 1

    def map_rescan_current(self, drop=None):
        self.rescans += 1
        return self._current_hit


def run_rescan(stub):
    return OSMap.map_rescan_once(stub, rescan_mode='full')


def test_probe_solved_while_panning_skips_the_whole_walk():
    stub = RescanStub(solved_in_pan=True)
    assert run_rescan(stub) is True
    # 一次队列都没走，站定判断也没跑
    assert stub.focuses == []
    assert stub.rescans == 1, stub.rescans
    # 钩子必须卸掉，别让后面的正常移动也带上探测
    assert stub._swipe_probe is None
    assert stub._rescan_probe_on is False
    assert stub._rescan_probe_drop is None


def test_probe_solved_while_focusing_skips_the_rest():
    stub = RescanStub(solved_at_focus=1)
    assert run_rescan(stub) is True
    assert stub.focuses == [(4, 1)]
    # 站定居中与站定判断都被跳过
    assert stub.centers == 0, stub.centers
    assert stub._swipe_probe is None


def test_walk_finishes_when_nothing_is_found():
    stub = RescanStub()
    assert run_rescan(stub) is False
    assert stub.focuses == [(4, 1), (4, 6), (4, 9)], stub.focuses
    assert stub.centers == 3, stub.centers
    # 一次当前视野 + 三站站定
    assert stub.rescans == 4, stub.rescans
    assert stub._swipe_probe is None
    assert stub._rescan_probe_on is False


def test_probe_is_removed_when_the_rescan_raises():
    stub = RescanStub(map_error=MapDetectionError('black screen'))
    raised = False
    try:
        run_rescan(stub)
    except MapDetectionError:
        raised = True
    assert raised
    assert stub._swipe_probe is None
    assert stub._rescan_probe_on is False


def test_nested_rescan_hands_the_probe_state_back_to_the_outer_one():
    """探测处理事件时会嵌套一次整图重扫（明石/装置够不着走强制移动）。

    内层收尾必须把外层的钩子、开关、掉落记录、已处理标记原样交还，否则外层剩下的
    镜头位置静默失去探测，掉落记录也少一帧。
    """
    stub = RescanStub()
    sentinel = lambda: False
    stub._swipe_probe = sentinel
    stub._rescan_probe_on = True
    stub._rescan_probe_drop = 'outer_drop'
    stub._rescan_probe_solved = True
    assert run_rescan(stub) is False
    assert stub._swipe_probe is sentinel
    assert stub._rescan_probe_on is True
    assert stub._rescan_probe_drop == 'outer_drop'
    assert stub._rescan_probe_solved is True
