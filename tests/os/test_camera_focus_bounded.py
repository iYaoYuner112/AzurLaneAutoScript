# -*- coding: utf-8 -*-
"""相机有界定位与失败滑动清理（对齐 AzurPilot 837cfea01）。

`focus_to()` 现在只把「有图像确认的位移」算作进展；连续 3 次无进展会先通过
地图边缘重新定位一次，仍失败就抛 `GameStuckError`，不再无限滑。
"""

import os
import sys
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.exception import GameStuckError
from module.map.camera import Camera


class FocusStub:
    def __init__(self, target, *, move=True):
        self.config = SimpleNamespace(MAP_SWIPE_DROP=0.2)
        self.view = SimpleNamespace(center_offset=np.array([0.5, 0.5]))
        self.camera = (0, 0)
        self.target = target
        self.move = move
        self._camera_confirm_swipe = False
        self._last_swipe_observed = None
        self._last_swipe_confirmed = False
        self._prev_view = None
        self._prev_swipe = None
        self.swipes = []
        self.recoveries = 0

    def map_swipe(self, vector):
        self.swipes.append(tuple(int(v) for v in vector))
        self._last_swipe_observed = None
        self._last_swipe_confirmed = False
        if self.move and tuple(vector) != (0, 0):
            self.camera = (self.camera[0] + int(vector[0]), self.camera[1] + int(vector[1]))
            self._last_swipe_confirmed = True
            return True
        return False

    def ensure_edge_insight(self, **kwargs):
        self.recoveries += 1
        return []


def test_focus_succeeds_with_confirmed_moves():
    stub = FocusStub((2, 0))
    Camera.focus_to(stub, (2, 0), swipe_limit=(1, 0))
    assert stub.camera == (2, 0), stub.camera
    assert stub._camera_confirm_swipe is False


def test_focus_raises_after_budget_and_one_recovery():
    stub = FocusStub((5, 0), move=False)
    try:
        Camera.focus_to(stub, (5, 0), swipe_limit=(1, 0))
    except GameStuckError:
        pass
    else:
        raise AssertionError('focus_to should raise GameStuckError')
    assert stub.recoveries == 1, stub.recoveries
    assert len(stub.swipes) == 6, stub.swipes
    assert stub._camera_confirm_swipe is False


class SwipeStub:
    def __init__(self):
        self.view = SimpleNamespace(center_offset=np.array([0.5, 0.5]))
        self._prev_view = object()
        self._prev_swipe = (1, 1)
        self._last_swipe_observed = object()
        self._last_swipe_confirmed = True

    def _map_swipe(self, vector):
        return False


def test_failed_swipe_clears_previous_frame():
    stub = SwipeStub()
    assert Camera.map_swipe(stub, (1, 0)) is False
    assert stub._prev_view is None
    assert stub._prev_swipe is None
    assert stub._last_swipe_observed is None
    assert stub._last_swipe_confirmed is False


if __name__ == '__main__':
    import unittest
    unittest.main()
