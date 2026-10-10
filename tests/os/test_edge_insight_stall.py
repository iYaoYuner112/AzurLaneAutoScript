"""大世界爬角时的滑动发散保护（`Camera.ensure_edge_insight`），对齐 AzurPilot 9c0a086a3。

问题：计划作战停下来之后要做整图重扫，先爬到地图角落标定镜头。视野里还没有边缘时，相机坐标
是**纯推算**（OS 里 `MAP_SWIPE_PREDICT=False`，没有主图那种反推真实滑动量的兜底），而游戏到边
就不再滚图、边界在暗处时边缘检测又报不出来 → 坐标一路白滑、永远不满足「两条边缘都在视野内」
→ 无限滑。

AP 的对策（提交 `9c0a086a3 fix(map/camera): 修复地图滑动后相机坐标持续发散的问题`，2026-09-06）：
滑动后锚点位移**不足半格**（`base / 2`）就认为该轴已经到边，停掉这条轴并撤销这一滑虚增的
相机坐标。**单帧判定**，没有连续帧确认，也没有滑动次数上限。

⚠️ **已知代价**（第 4 个用例专门锁住，别顺手"修正"）：`homo_loca` 在
`homography.load()` 里被 `%= HOMO_TILE`（140 像素）取模，所以一次**合法**滑动留下的正好是
那个小数残差（2026-10-05 实测 9~19 像素 ≈ 0.1 格）。相位没跨过 140 时它也 < 70 像素
→ **同样会被判成「到头」**，提前停掉该轴。这是对齐 AP 的代价；改阈值又会和 AP 不一致。
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
    判据本身就在被测方法里，所以这里不再借真实现的任何判据函数。
    """

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
    """画面每帧都在滚（位移远超半格）、边缘报出来：一帧都不许多停，也不退回坐标。"""
    stub = PanStub([frame(), frame(edges=(False, True, True, False))])
    record = run_pan(stub)
    assert stub.gestures == [(3, 2), (3, 2)], stub.gestures
    assert stub.camera == (9, 7), stub.camera
    assert record[-1] == (0, 0)


def test_one_stalled_swipe_is_enough_to_stop_that_axis():
    """AP 是单帧判据：一滑没滚图就停轴 + 撤销**这一滑**虚增的坐标（不需要连续两帧）。"""
    stub = PanStub([frame(), frame(move=(0.0, 0.0))])
    record = run_pan(stub)
    # 第一滑正常滚图 → 相机 (6, 5)；第二滑画面没动 → 两条轴一起停掉，
    # 并把这一滑虚增的 (3, 2) 退回去（所以相机停在 6, 5，而不是继续漂到 9, 7）
    assert stub.gestures == [(3, 2), (3, 2)], stub.gestures
    assert stub.camera == (6, 5), stub.camera
    # 停轴后下一轮 x == y == 0，循环收工
    assert record[-1] == (0, 0), record


def test_stalled_axis_does_not_stop_the_other():
    """横向停在原地、纵向还在滚：只停横向并只退横向那一滑的坐标，纵向照常继续。"""
    stub = PanStub([
        frame(move=(0.0, 2.0)),
        frame(edges=(False, True, True, False), move=(0.0, 2.0)),
    ])
    run_pan(stub)
    # 第一滑只有 x 被停掉（相机 x 从 6 退回 3），y 继续累加
    assert stub.gestures == [(3, 2), (0, 2)], stub.gestures
    assert stub.camera == (3, 7), stub.camera


def test_sub_tile_residual_counts_as_the_edge():
    """⚠️ 对齐 AP 的已知代价：合法滑动留下的小残差也会被判成「到头」。

    `homo_loca` 取模到 140 像素，一次合法滑动（3 格 + 一点点）留下的正好是那一点点
    （2026-10-05 实测 9~19 像素）。AP 的阈值是半格 = 70 像素，所以这点残差同样命中 →
    提前停轴，并把这一滑的坐标退回去（这里 0.1 格 ≈ 14 像素，实测残差量级）。
    AP 只有这一条判据（没有连续帧确认、没有次数上限），所以这里如实锁住该行为；
    不要为了"更准"去改阈值，否则又和 AP 不一致。
    """
    stub = PanStub([frame(move=(0.1, 0.1))])
    run_pan(stub)
    # 只有第一滑真的发生；它被当成「到头」，两条轴一起停，坐标退回到起点
    assert stub.gestures == [(3, 2)], stub.gestures
    assert stub.camera == (3, 3), stub.camera
