"""行动力保留线的优雅处理（对齐 AP master）。

- `ActionPointLimit` 异常携带现场数据（current/total/cost/preserve）与 `delay_minutes`；
- 短猫独立运行（智能调度关闭）且黄币足够时，行动力打到保留线 → 推迟到服务器刷新；
- 智能调度代理子任务时，调度层捕获 `ActionPointLimit` → 推迟到服务器刷新，
  而不是让任务以异常方式失败。
"""

from types import SimpleNamespace
from contextlib import contextmanager

import module.os.tasks.scheduling as sched_mod
from module.os.tasks.scheduling import OpsiScheduling
from module.os.tasks.meowfficer_farming import OpsiMeowfficerFarming
from module.os_handler.action_point import ActionPointLimit


class _TempContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recover(self):
        pass


# ---- ActionPointLimit 字段 ----

def test_action_point_limit_carries_fields():
    e = ActionPointLimit(current=1000, total=3400, cost=120, preserve=200)
    assert e.current == 1000
    assert e.total == 3400
    assert e.cost == 120
    assert e.preserve == 200


def test_action_point_limit_delay_minutes():
    assert ActionPointLimit(current=1000, cost=1200).delay_minutes == 2000
    assert ActionPointLimit(current=1300, cost=1200).delay_minutes is None
    assert ActionPointLimit().delay_minutes is None


def test_action_point_limit_can_be_raised_bare():
    """兼容旧的裸 raise（字段为 None）。"""
    try:
        raise ActionPointLimit
    except ActionPointLimit as e:
        assert e.current is None and e.total is None


# ---- 短猫行动力检查 ----

class MeowApCheckStub:
    is_smart_scheduling_enabled = False
    is_cl1_mode_enabled = True
    yellow_coins_preserve = 20000

    def __init__(self, yellow=30000, raise_limit=True, buy=False):
        self.delayed = []
        self.stopped = []
        self.config = SimpleNamespace(
            temporary=lambda **kwargs: _TempContext(),
            OpsiGeneral_BuyActionPointLimit=buy,
            OS_ACTION_POINT_PRESERVE=0,
            task=SimpleNamespace(command='OpsiMeowfficerFarming'),
            task_delay=lambda **kwargs: self.delayed.append(kwargs),
            task_stop=lambda: self.stopped.append(1),
        )
        self._yellow = yellow
        self._raise_limit = raise_limit
        self.ap_set_calls = []

    def get_yellow_coins(self):
        return self._yellow

    def action_point_set(self, **kwargs):
        self.ap_set_calls.append(kwargs)
        if self._raise_limit:
            raise ActionPointLimit(
                current=1000, total=self.config.OS_ACTION_POINT_PRESERVE,
                preserve=self.config.OS_ACTION_POINT_PRESERVE)

    _meow_ap_check = OpsiMeowfficerFarming._meow_ap_check


def test_meow_postpones_to_server_update_on_limit():
    """独立跑短猫（非智能调度）+ 黄币足够：打到保留线 → 推迟到服务器刷新。"""
    stub = MeowApCheckStub(yellow=30000, raise_limit=True)
    assert OpsiMeowfficerFarming._meow_ap_check(stub, False) is True
    assert stub.delayed == [{'server_update': True}]
    assert len(stub.stopped) == 1


def test_meow_propagates_limit_when_yellow_not_enough():
    """黄币不够时不推迟：异常按 master 的原样向上抛。"""
    stub = MeowApCheckStub(yellow=100, raise_limit=True)
    raised = False
    try:
        OpsiMeowfficerFarming._meow_ap_check(stub, False)
    except ActionPointLimit:
        raised = True
    assert raised
    assert stub.delayed == []


def test_meow_ap_check_passes_when_ap_enough():
    stub = MeowApCheckStub(yellow=30000, raise_limit=False)
    assert OpsiMeowfficerFarming._meow_ap_check(stub, False) is True
    assert stub.delayed == []


def test_meow_ap_check_second_round_skips_check():
    stub = MeowApCheckStub(yellow=30000, raise_limit=True)
    assert OpsiMeowfficerFarming._meow_ap_check(stub, True) is True
    assert stub.ap_set_calls == []
    assert stub.delayed == []


# ---- 调度代理层捕获 ActionPointLimit ----

class _StopCounter:
    n = 0


class ProxyStub:
    def __init__(self, raise_limit=True):
        self.delayed = []
        self.stop_counter = _StopCounter()
        self.postpones = []
        self.config = SimpleNamespace(
            task_delay=lambda **kwargs: self.delayed.append(kwargs),
            task_stop=lambda: setattr(self.stop_counter, 'n', self.stop_counter.n + 1),
        )
        self._raise_limit = raise_limit

    def _get_coin_task_handler(self, task_name):
        def handler():
            if self._raise_limit:
                raise ActionPointLimit(current=1000, total=1000, preserve=1000)
        return handler

    def _postpone_coin_task_check(self, task_name, reason):
        self.postpones.append((task_name, reason))

    _run_scheduled_coin_task_once = OpsiScheduling._run_scheduled_coin_task_once


def test_scheduler_proxy_catches_action_point_limit():
    import module.os.tasks.scheduling as sched_mod

    original_ctx = sched_mod.opsi_task_context
    original_pop = sched_mod.pop_opsi_no_content

    @contextmanager
    def fake_ctx(config, task_name, disable_task_switch=True):
        yield

    stub = ProxyStub(raise_limit=True)
    sched_mod.opsi_task_context = fake_ctx
    sched_mod.pop_opsi_no_content = lambda config: None
    try:
        result = OpsiScheduling._run_scheduled_coin_task_once(
            stub, 'OpsiMeowfficerFarming', 0)
    finally:
        sched_mod.opsi_task_context = original_ctx
        sched_mod.pop_opsi_no_content = original_pop

    assert result.status == sched_mod.OpsiStatus.FAILED
    assert result.reason == 'action point limit'
    assert stub.delayed == [{'server_update': True}]
    assert stub.stop_counter.n == 1


def test_scheduler_proxy_success_path_unaffected():
    import module.os.tasks.scheduling as sched_mod

    original_ctx = sched_mod.opsi_task_context
    original_pop = sched_mod.pop_opsi_no_content

    @contextmanager
    def fake_ctx(config, task_name, disable_task_switch=True):
        yield

    stub = ProxyStub(raise_limit=False)
    sched_mod.opsi_task_context = fake_ctx
    sched_mod.pop_opsi_no_content = lambda config: None
    try:
        result = OpsiScheduling._run_scheduled_coin_task_once(
            stub, 'OpsiMeowfficerFarming', 0)
    finally:
        sched_mod.opsi_task_context = original_ctx
        sched_mod.pop_opsi_no_content = original_pop

    assert result.status == sched_mod.OpsiStatus.SUCCESS
    assert stub.delayed == []
