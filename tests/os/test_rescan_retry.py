"""整图重扫读不到地图时的处理（对齐 AzurPilot 的实测结论，但不学它放弃得那么狠）。

AP 的注释写着：短猫把海域打完之后单应性变换本来就找不到有效格子，那是**正常状态**不是黑帧。
我们旧代码把它当黑帧，最多重试 5 整轮（失败发生在爬完角之后时≈60 秒），然后把
`MapDetectionError` 抛穿打死任务——一个已经清完的海域因此既白花时间又挂任务。

现在的口径：截图确认还在地图界面就最多再试 `_RESCAN_TRIALS` 次，用尽后警告"可能有事件没
扫到"并正常收工交给上层；一发现画面已经不是地图（弹窗/别的任务接管）就立刻上抛，让界面的
错误处理去复位，免得拿着错的画面继续点。
"""

from types import SimpleNamespace

from module.exception import MapDetectionError
from module.os.map import OSMap


class RetryStub:
    """`map_rescan()` 需要的最小对象：按剧本让每遍重扫成功、没找到、或者读不到图。"""

    _RESCAN_TRIALS = OSMap._RESCAN_TRIALS

    def __init__(self, script, in_map=True, solve_on_success=False):
        self.script = list(script)
        self.in_map = in_map
        self._solve_on_success = solve_on_success
        self.config = SimpleNamespace(OpsiFleet_Fleet=1)
        self.zone = SimpleNamespace(is_port=False)
        self.is_in_task_cl1_leveling = False
        self.is_in_task_explore = False
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False
        self.screenshots = 0
        self.calls = 0
        self.fleets = []

    def fleet_set(self, index=1):
        self.fleets.append(index)
        return True

    def get_second_fleet(self):
        return 2

    def device_screenshot(self):
        self.screenshots += 1

    def is_in_map(self):
        return self.in_map

    def map_rescan_once(self, rescan_mode='full', drop=None):
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if item == 'error':
            raise MapDetectionError('Failed to find a free tile')
        if item and self._solve_on_success:
            self._solved_map_event = {'is_akashi'}
        return bool(item)


def make(script, **kwargs):
    stub = RetryStub(script, **kwargs)
    stub.device = SimpleNamespace(screenshot=stub.device_screenshot)
    return stub


def test_cleared_map_no_longer_kills_the_task():
    """读不到有效格子但画面还在地图上：试完就收工，不把异常抛穿。"""
    stub = make(['error'] * 10)
    assert OSMap.map_rescan(stub) is False
    assert stub.calls == OSMap._RESCAN_TRIALS, stub.calls
    assert stub.screenshots == OSMap._RESCAN_TRIALS, stub.screenshots
    # 收工前要把主队切回去，别把后面的流程留在别的舰队上
    assert stub.fleets[-1] == 1


def test_trial_count_is_three_not_five():
    """旧上限 5 轮≈60 秒，现在 3 轮。"""
    assert OSMap._RESCAN_TRIALS == 3


def test_frame_that_is_not_the_map_is_handed_up_immediately():
    """画面已经不是地图（弹窗/别的任务接管）：一次都不重试，直接上抛。"""
    stub = make(['error'] * 10, in_map=False)
    raised = False
    try:
        OSMap.map_rescan(stub)
    except MapDetectionError:
        raised = True
    assert raised
    assert stub.calls == 1, stub.calls


def test_transient_black_frame_still_gets_a_second_chance():
    """第一遍黑帧、第二遍读得出来：照常把这轮扫完，返回"没有更多事件"。"""
    stub = make(['error', False])
    assert OSMap.map_rescan(stub) is True
    assert stub.calls == 2, stub.calls
    assert stub.screenshots == 1, stub.screenshots


def test_solved_events_still_rescan_next_pass():
    """每遍都处理掉一个事件时继续扫下一遍，直到某遍什么都没有。"""
    stub = make([True, True, False])
    assert OSMap.map_rescan(stub) is True
    assert stub.calls == 3, stub.calls


def test_solved_event_stops_the_loop_outside_explore():
    """非开荒时处理掉一个事件就不再扫第二遍（这条行为不许被改坏）。"""
    stub = make([True, True, True], solve_on_success=True)
    assert OSMap.map_rescan(stub) is False
    assert stub.calls == 1, stub.calls
