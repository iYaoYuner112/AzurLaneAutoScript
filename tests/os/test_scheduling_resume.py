"""智能调度被其他任务打断后，恢复时多扫一次图。

OpsiScheduling 把「上一次运行是否被任务切换打断」持久化到
OpsiScheduling.Storage.Storage 的 SchedulingInterrupted 字段：
- 正常退出（等待行动力 / 子任务被禁用 / 探索中）会清掉这个标记；
- 被 check_task_switch 打断时标记保留到下一次进入；
- 恢复进入时读到标记，在第一次练级/短猫回到打捞海域后多扫一次图。

这里只锁定两部分：标记的读写往返，以及 consume_resume_extra_scan 的消费行为。
"""

from types import SimpleNamespace

from module.os.map import OSMap
from module.os.tasks.scheduling import OpsiScheduling


class FakeConfig:
    """只提供 cross_get / cross_set 的最小配置替身。"""

    def __init__(self):
        self.storage = {}

    def cross_get(self, keys, default=None):
        return self.storage.get(keys, default)

    def cross_set(self, keys, value):
        self.storage[keys] = value

    def update(self):
        pass


class FakeScheduler:
    """给 OpsiScheduling 的未绑定方法一个可用的 self。"""

    def __init__(self):
        self.config = FakeConfig()

    def _scheduling_storage(self):
        return OpsiScheduling._scheduling_storage(self)


def test_interrupted_flag_defaults_to_false():
    scheduler = FakeScheduler()
    assert OpsiScheduling._scheduling_interrupted(scheduler) is False


def test_interrupted_flag_roundtrip():
    scheduler = FakeScheduler()
    OpsiScheduling._set_scheduling_interrupted(scheduler, True)
    assert OpsiScheduling._scheduling_interrupted(scheduler) is True
    OpsiScheduling._set_scheduling_interrupted(scheduler, False)
    assert OpsiScheduling._scheduling_interrupted(scheduler) is False


def test_interrupted_flag_survives_in_same_storage_dict():
    """标记写在 Storage.Storage 里，不覆盖同键已有的其它字段。"""
    scheduler = FakeScheduler()
    scheduler.config.storage['OpsiScheduling.Storage.Storage'] = {
        'CoinReplenishActive': True,
    }
    OpsiScheduling._set_scheduling_interrupted(scheduler, True)
    state = scheduler.config.storage['OpsiScheduling.Storage.Storage']
    assert state['CoinReplenishActive'] is True
    assert state['SchedulingInterrupted'] is True


def test_consume_resume_extra_scan_runs_once_when_flagged():
    calls = []
    stub = SimpleNamespace(
        _resume_extra_scan=True,
        map_rescan=lambda **kw: calls.append(kw),
    )
    OSMap.consume_resume_extra_scan(stub)
    assert calls == [{'rescan_mode': 'full'}]
    assert stub._resume_extra_scan is False
    # Second call is a no-op: the flag is already consumed.
    OSMap.consume_resume_extra_scan(stub)
    assert calls == [{'rescan_mode': 'full'}]


def test_consume_resume_extra_scan_resets_scan_state():
    """扫图前要把上一轮残留的已解决事件清空，避免误判已解决。"""
    calls = []
    stub = SimpleNamespace(
        _resume_extra_scan=True,
        _solved_map_event={'is_akashi'},
        _solved_fleet_mechanism=True,
        map_rescan=lambda **kw: calls.append(kw),
    )
    OSMap.consume_resume_extra_scan(stub)
    assert calls == [{'rescan_mode': 'full'}]
    assert stub._solved_map_event == set()
    assert stub._solved_fleet_mechanism is False


def test_consume_resume_extra_scan_noop_when_not_flagged():
    calls = []
    stub = SimpleNamespace(
        _resume_extra_scan=False,
        map_rescan=lambda **kw: calls.append(kw),
    )
    OSMap.consume_resume_extra_scan(stub)
    assert calls == []


def test_consume_resume_extra_scan_noop_when_flag_missing():
    """子任务独立运行时没有这个标志，不能抛 AttributeError。"""
    calls = []
    stub = SimpleNamespace(map_rescan=lambda **kw: calls.append(kw))
    OSMap.consume_resume_extra_scan(stub)
    assert calls == []
