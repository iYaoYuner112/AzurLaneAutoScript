"""大世界爬角时的滑动发散保护（`Camera.ensure_edge_insight`）。

计划作战停下来之后要做整图重扫：先爬到地图角落标定镜头。旧代码有个洞——

滑动过程只判断"到没到边"，从不判断"画面有没有真的滚"。游戏到边就不再滚图，而边界又是
暗的时候边缘检测报不出来，相机坐标却每轮照滑动量累加，于是一路白滑（OS 里
`MAP_SWIPE_PREDICT=False`，没有主图那种反推真实滑动量的兜底）。现在连续两帧画面不动才
停轴并退回虚增坐标；单帧不算，因为锚点是按一格取模的相位，正好滚完整数格时位移也是 0。
另有滑动次数硬上限。

（原文件还覆盖过"滑动后顺手处理路过事件"的重扫探针，那套已按对齐 AP 的要求删除。）
"""

import numpy as np
from types import SimpleNamespace

from module.map.camera import Camera

REAL = (3.0, 2.0)
NO_EDGE = (False, False, False, False)
ALL_EDGE = (True, True, True, True)


def frame(edges=NO_EDGE, move=REAL):
    """一帧剧本：edges=(左, 右, 上, 下) 有没有被检测到，move=这一滑画面真实滚了多少格。"""
    return {'edges': edges, 'move': move}


class PanStub:
    """`Camera.ensure_edge_insight()` 需要的最小对象。

    `map_swipe()` 按剧本改写边缘与同位锚点，并像真实现那样把滑动量累加进相机坐标。
    """

    SWIPE_STALL_RATIO = Camera.SWIPE_STALL_RATIO
    SWIPE_STALL_FRAMES = Camera.SWIPE_STALL_FRAMES
    EDGE_INSIGHT_SWIPE_LIMIT = Camera.EDGE_INSIGHT_SWIPE_LIMIT
    # 借用真实现的两个小工具，免得测试和代码各写一份判据
    _swipe_moved_grids = Camera._swipe_moved_grids
    _swipe_stall_check = Camera._swipe_stall_check

    def __init__(self, frames=(), corner='bottom-right', inplace=False):
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
