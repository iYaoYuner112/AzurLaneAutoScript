"""Opsi 调度决策层单测：任务选择是状态驱动的，而不是固定顺序。

覆盖规格第二十九节的核心场景：
- 黄币补充任务按 TaskPriority 排序、受 Enable 开关约束；
- 一次调度只代理“第一个有内容”的任务一轮，其余任务不被连带执行；
- 无内容（NO_CONTENT）不是失败：跳过并尝试下一个 / 本轮延迟；
- 已清空（postpone）的任务被跳过；
- 调度模式切换时清理旧模式状态；
- 子任务延迟归属真正的拥有者（调度器），不改自己的 schedule；
- 开荒运行期间调度让路。
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from module.config.config import TaskEnd
from module.os.tasks.scheduling import (
    OpsiScheduling,
    SchedulingMode,
    STATE_KEY_AP_REPLENISH,
    STATE_KEY_COIN_REPLENISH,
    STATE_KEY_SCHEDULING_MODE,
)
from module.os.tasks.task_context import (
    OpsiNoContent,
    OpsiStatus,
    OpsiTaskContext,
    TaskDelayRequest,
    is_running_opsi_proxy,
    opsi_no_content,
    should_hand_over_to_scheduling,
)

CONFIG_PATH_SMART_STATE = 'OpsiScheduling.Storage.Storage'


class FakeConfig:
    def __init__(self, values=None, task='OpsiScheduling'):
        self.values = dict(values or {})
        self.task = SimpleNamespace(command=task)

    def cross_get(self, keys, default=None):
        return self.values.get(keys, default)

    def cross_set(self, keys, value):
        self.values[keys] = value


def bind(stub, *names):
    """Bind the real OpsiScheduling methods onto a lightweight stub."""
    for name in names:
        def make(method_name):
            return lambda *args, **kwargs: getattr(OpsiScheduling, method_name)(stub, *args, **kwargs)
        setattr(stub, name, make(name))
    return stub


def make_scheduler(values=None):
    stub = SimpleNamespace(config=FakeConfig(values))
    bind(
        stub,
        '_get_smart_state', '_save_smart_state', '_sync_scheduling_mode',
        '_get_task_priority', '_is_coin_task_enabled', '_get_enabled_coin_tasks',
        '_postpone_coin_task_check', '_get_coin_task_postpone',
    )
    return stub


# ---- 任务选择：TaskPriority 与 Enable ----

def test_coin_tasks_ordered_by_task_priority():
    stub = make_scheduler({
        'OpsiScheduling.OpsiScheduling.TaskPriority': 'OpsiMeowfficerFarming > OpsiStronghold',
    })
    assert OpsiScheduling._get_enabled_coin_tasks(stub) == [
        'OpsiMeowfficerFarming', 'OpsiStronghold', 'OpsiObscure', 'OpsiAbyssal',
    ]


def test_coin_tasks_respect_enable_flags():
    stub = make_scheduler({
        'OpsiScheduling.OpsiScheduling.EnableObscure': False,
        'OpsiScheduling.OpsiScheduling.EnableStronghold': False,
    })
    assert OpsiScheduling._get_enabled_coin_tasks(stub) == [
        'OpsiAbyssal', 'OpsiMeowfficerFarming',
    ]


def test_invalid_priority_falls_back_to_default_order():
    stub = make_scheduler({'OpsiScheduling.OpsiScheduling.TaskPriority': ''})
    assert OpsiScheduling._get_enabled_coin_tasks(stub) == [
        'OpsiStronghold', 'OpsiObscure', 'OpsiAbyssal', 'OpsiMeowfficerFarming',
    ]


# ---- 一次只代理一个任务 ----

def test_dispatch_picks_first_candidate_with_content():
    stub = make_scheduler()
    calls = []

    def run_once(task_name, ap_preserve, fresh_ap=None):
        calls.append(task_name)
        if task_name == 'OpsiStronghold':
            return SimpleNamespace(executed=False, status=OpsiStatus.NO_CONTENT, task=task_name)
        return SimpleNamespace(executed=True, status=OpsiStatus.SUCCESS, task=task_name)

    stub._run_scheduled_coin_task_once = run_once
    result = OpsiScheduling._dispatch_coin_task(stub, 100, 1000, 1000, 1000)
    # Stronghold had no content -> only Obscure is executed, nothing else.
    assert calls == ['OpsiStronghold', 'OpsiObscure']
    assert result.task == 'OpsiObscure'
    assert result.executed is True


def test_dispatch_returns_no_content_when_every_candidate_is_empty():
    stub = make_scheduler()
    stub._run_scheduled_coin_task_once = lambda name, ap, fresh_ap=None: SimpleNamespace(
        executed=False, status=OpsiStatus.NO_CONTENT, task=name)
    result = OpsiScheduling._dispatch_coin_task(stub, 100, 1000, 1000, 1000)
    assert result.status == OpsiStatus.NO_CONTENT
    assert result.executed is False


def test_dispatch_skips_postponed_candidates():
    stub = make_scheduler()
    calls = []
    stub._run_scheduled_coin_task_once = lambda name, ap, fresh_ap=None: (
        calls.append(name) or SimpleNamespace(executed=True, status=OpsiStatus.SUCCESS, task=name))
    future = datetime.now() + timedelta(hours=3)
    stub._get_coin_task_postpone = lambda name: future if name == 'OpsiStronghold' else None
    result = OpsiScheduling._dispatch_coin_task(stub, 100, 1000, 1000, 1000)
    assert calls == ['OpsiObscure']
    assert result.task == 'OpsiObscure'


def test_dispatch_reports_no_task_when_nothing_enabled():
    stub = make_scheduler({
        'OpsiScheduling.OpsiScheduling.EnableStronghold': False,
        'OpsiScheduling.OpsiScheduling.EnableObscure': False,
        'OpsiScheduling.OpsiScheduling.EnableAbyssal': False,
        'OpsiScheduling.OpsiScheduling.EnableMeowfficerFarming': False,
    })
    result = OpsiScheduling._dispatch_coin_task(stub, 100, 1000, 1000, 1000)
    assert result.status == OpsiStatus.NO_TASK
    assert result.executed is False


def test_postpone_is_recorded_and_expires():
    stub = make_scheduler()
    OpsiScheduling._postpone_coin_task_check(stub, 'OpsiObscure', 'cleared')
    state = stub.config.values[CONFIG_PATH_SMART_STATE]
    assert 'ObscureNextCheck' in state
    assert OpsiScheduling._get_coin_task_postpone(stub, 'OpsiObscure') is not None
    # An expired record is dropped and the task becomes eligible again.
    stub.config.values[CONFIG_PATH_SMART_STATE]['ObscureNextCheck'] = (
        datetime.now() - timedelta(minutes=1)).isoformat()
    assert OpsiScheduling._get_coin_task_postpone(stub, 'OpsiObscure') is None
    assert 'ObscureNextCheck' not in stub.config.values[CONFIG_PATH_SMART_STATE]


# ---- 结果语义 ----

def test_no_content_is_not_a_failure():
    empty = SimpleNamespace(status=OpsiStatus.NO_CONTENT)
    from module.os.tasks.task_context import OpsiTaskResult
    assert OpsiTaskResult(OpsiStatus.NO_CONTENT).no_content is True
    assert OpsiTaskResult(OpsiStatus.NO_CONTENT).executed is False
    assert OpsiTaskResult(OpsiStatus.FAILED).executed is False
    assert OpsiTaskResult(OpsiStatus.FAILED).no_content is False
    assert OpsiTaskResult(OpsiStatus.SUCCESS).executed is True
    assert empty.status == OpsiStatus.NO_CONTENT


# ---- 状态机 ----

def test_mode_switch_clears_previous_mode_state():
    stub = make_scheduler({CONFIG_PATH_SMART_STATE: {
        STATE_KEY_SCHEDULING_MODE: SchedulingMode.COIN_TARGET,
        STATE_KEY_COIN_REPLENISH: True,
        STATE_KEY_AP_REPLENISH: True,
    }})
    state = OpsiScheduling._get_smart_state(stub)
    changed = OpsiScheduling._sync_scheduling_mode(stub, state, SchedulingMode.NORMAL)
    assert changed is True
    assert state[STATE_KEY_SCHEDULING_MODE] == SchedulingMode.NORMAL
    # Old mode state must not leak into the new mode.
    assert STATE_KEY_COIN_REPLENISH not in state
    assert STATE_KEY_AP_REPLENISH not in state


def test_same_mode_keeps_state():
    stub = make_scheduler({CONFIG_PATH_SMART_STATE: {
        STATE_KEY_SCHEDULING_MODE: SchedulingMode.COIN_TARGET,
        STATE_KEY_COIN_REPLENISH: True,
    }})
    state = OpsiScheduling._get_smart_state(stub)
    changed = OpsiScheduling._sync_scheduling_mode(stub, state, SchedulingMode.COIN_TARGET)
    assert changed is False
    assert state[STATE_KEY_COIN_REPLENISH] is True


# ---- 延迟归属 ----

def test_delay_request_is_applied_to_the_owner_task():
    calls = []

    class Cfg:
        def task_delay(self, **kwargs):
            calls.append(kwargs)

    TaskDelayRequest(minute=150, server_update=True, task='OpsiScheduling').apply(Cfg())
    assert calls == [{
        'success': None, 'server_update': True, 'target': None,
        'minute': 150, 'task': 'OpsiScheduling',
    }]


def test_empty_delay_request_is_ignored():
    calls = []

    class Cfg:
        def task_delay(self, **kwargs):
            calls.append(kwargs)

    assert TaskDelayRequest().apply(Cfg()) is False
    assert calls == []


# ---- no-content 语义：代理内抛出，独立运行沿用旧行为 ----

def test_opsi_no_content_marks_and_raises_under_proxy():
    config = SimpleNamespace(
        _opsi_context=OpsiTaskContext(parent_task='OpsiScheduling', current_task='OpsiStronghold'))
    assert is_running_opsi_proxy(config) is True
    try:
        opsi_no_content(config, 'OpsiStronghold', 'No siren stronghold')
    except OpsiNoContent:
        pass
    else:
        raise AssertionError('expected OpsiNoContent under a proxy')
    assert config._opsi_no_content_task == 'OpsiStronghold'


def test_opsi_no_content_keeps_legacy_behaviour_standalone():
    delays = []

    class Cfg:
        def task_delay(self, **kwargs):
            delays.append(kwargs)

        def task_stop(self):
            raise TaskEnd

    config = Cfg()
    try:
        opsi_no_content(config, 'OpsiObscure', 'No obscure coordinates', minute=150)
    except TaskEnd:
        pass
    else:
        raise AssertionError('expected TaskEnd standalone')
    assert delays == [{'minute': 150, 'server_update': True}]


# ---- 独立运行时的交接 ----

def test_hand_over_requires_enabled_coin_task_and_not_proxying():
    enabled = SimpleNamespace(cross_get=lambda keys, default=None: True)
    assert should_hand_over_to_scheduling(enabled, 'OpsiObscure', True) is True
    assert should_hand_over_to_scheduling(enabled, 'OpsiObscure', False) is False
    # A task the scheduler does not manage must never hand over.
    assert should_hand_over_to_scheduling(enabled, 'OpsiDaily', True) is False

    disabled = SimpleNamespace(
        cross_get=lambda keys, default=None: False if 'EnableObscure' in keys else default)
    assert should_hand_over_to_scheduling(disabled, 'OpsiObscure', True) is False
