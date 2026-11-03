"""`Camera.ensure_edge_insight()` 的有界爬角行为（对齐 AzurPilot 837cfea01）。

AP 在 2026-10-31 去掉了旧的 `homo_loca` 半格判据：`homo_loca` 是取模后的格内
偏移，合法滑动也可能不变，不能用来判断地图是否到边。现在最多滑 12 次，
仍然看不到两条地图边缘就抛 `GameStuckError`，交给上层重启恢复，避免坐标
一路白滑、永远不结束。

⚠️ 旧的「单帧半格残差就停轴」行为已被 AP 删除，不要再加回来。
"""

from types import SimpleNamespace

from module.exception import GameStuckError
from module.map.camera import Camera

NO_EDGE = (False, False, False, False)
ALL_EDGE = (True, True, True, True)


def frame(edges=NO_EDGE):
    return {'edges': edges}


class PanStub:
    """`ensure_edge_insight()` 需要的最小对象。"""

    def __init__(self, frames=(), corner='bottom-right', edges=NO_EDGE):
        self.config = SimpleNamespace(MAP_ENSURE_EDGE_INSIGHT_CORNER=corner)
        self.view = SimpleNamespace(
            left_edge=edges[0], right_edge=edges[1],
            upper_edge=edges[2], lower_edge=edges[3],
        )
        self.frames = list(frames)
        self.camera = (3, 3)
        self.gestures = []

    def _spec(self):
        if not self.frames:
            return frame()
        return self.frames[min(len(self.gestures) - 1, len(self.frames) - 1)]

    def map_swipe(self, vector):
        vector = tuple(int(v) for v in vector)
        if vector == (0, 0):
            return False
        self.gestures.append(vector)
        left, right, upper, lower = self._spec()['edges']
        self.view.left_edge, self.view.right_edge = left, right
        self.view.upper_edge, self.view.lower_edge = upper, lower
        self.camera = (self.camera[0] + vector[0], self.camera[1] + vector[1])
        return True


def run_pan(stub, **kwargs):
    return Camera.ensure_edge_insight(stub, **kwargs)


def test_edges_seen_after_one_swipe():
    """滑一次就看到右/上边缘：停止，不再额外多滑一帧。"""
    stub = PanStub([frame(edges=(False, True, True, False))])
    record = run_pan(stub)
    assert stub.gestures == [(3, 2)], stub.gestures
    assert stub.camera == (6, 5), stub.camera
    assert record == [(3, 2)], record


def test_both_edges_already_visible_does_not_swipe():
    stub = PanStub(edges=ALL_EDGE)
    record = run_pan(stub)
    assert stub.gestures == [], stub.gestures
    assert record == [], record


def test_no_edges_raises_after_bounded_swipes():
    """看不到边缘时最多滑 12 次，然后交给上层重启，不能无限滑。"""
    stub = PanStub()
    try:
        run_pan(stub)
    except GameStuckError:
        pass
    else:
        raise AssertionError('ensure_edge_insight should raise GameStuckError')
    assert len(stub.gestures) == 12, stub.gestures
    assert stub.camera == (3 + 12 * 3, 3 + 12 * 2), stub.camera


def test_small_residual_no_longer_counts_as_edge():
    """旧的半格残差判据已删除；只要边缘没识别出来就继续滑，最终报卡死。"""
    stub = PanStub([frame(edges=NO_EDGE)])
    try:
        run_pan(stub)
    except GameStuckError:
        pass
    else:
        raise AssertionError('small residual must not be treated as an edge anymore')
    assert len(stub.gestures) == 12, stub.gestures


def test_reverse_replays_the_record():
    stub = PanStub([frame(edges=(False, True, True, False))])
    run_pan(stub, reverse=True)
    assert stub.gestures == [(3, 2), (-3, -2)], stub.gestures
    assert stub.camera == (3, 3), stub.camera
