"""行动力保留线的优雅处理（对齐 AP master）。

- `ActionPointLimit` 异常携带现场数据（current/total/cost/preserve）与 `delay_minutes`；
- 短猫独立运行（智能调度关闭）且黄币足够时，行动力打到保留线 → 推迟到服务器刷新；
- 智能调度代理子任务时，调度层捕获 `ActionPointLimit` → 推迟到服务器刷新，
  而不是让任务以异常方式失败。
"""

from types import SimpleNamespace
from datetime import datetime as dt, timedelta
from contextlib import contextmanager
from unittest.mock import patch

import module.os.tasks.scheduling as sched_mod
from module.campaign.os_run import OSCampaignRun
from module.os.map import OSMap
from module.os.tasks.scheduling import OpsiScheduling
from module.os.tasks.meowfficer_farming import OpsiMeowfficerFarming
from module.os_handler.os_status import OSStatus
from module.config.utils import get_server_next_update
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

    def __init__(self, yellow=30000, raise_limit=True, buy=False, cl1_preserve=20000):
        self.delayed = []
        self.stopped = []
        self.config = SimpleNamespace(
            temporary=lambda **kwargs: _TempContext(),
            OpsiGeneral_BuyActionPointLimit=buy,
            OpsiHazard1Leveling_OperationCoinsPreserve=cl1_preserve,
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

    @property
    def cl1_enough_yellow_coins(self):
        return self._yellow >= self.config.OpsiHazard1Leveling_OperationCoinsPreserve

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


def test_meow_uses_hazard1_operation_coins_preserve():
    """短猫独立跑的“黄币够不够”读侵蚀一页的保留凭证。"""
    stub = MeowApCheckStub(yellow=30000, raise_limit=True, cl1_preserve=40000)
    raised = False
    try:
        OpsiMeowfficerFarming._meow_ap_check(stub, False)
    except ActionPointLimit:
        raised = True
    assert raised
    assert stub.delayed == []

    stub = MeowApCheckStub(yellow=40000, raise_limit=True, cl1_preserve=40000)
    assert OpsiMeowfficerFarming._meow_ap_check(stub, False) is True
    assert stub.delayed == [{'server_update': True}]


class CoinEnoughStub:
    cl1_enough_yellow_coins = OSStatus.cl1_enough_yellow_coins

    def __init__(self, yellow, preserve):
        self._yellow = yellow
        self.config = SimpleNamespace(
            OpsiHazard1Leveling_OperationCoinsPreserve=preserve,
        )

    def get_yellow_coins(self):
        return self._yellow


def test_cl1_enough_yellow_coins_uses_the_task_reserve():
    assert CoinEnoughStub(yellow=40000, preserve=40000).cl1_enough_yellow_coins
    assert not CoinEnoughStub(yellow=39999, preserve=40000).cl1_enough_yellow_coins


class Cl1ApPreserveStub:
    cl1_ap_preserve = OSMap.cl1_ap_preserve
    is_smart_scheduling_enabled = False
    is_cl1_enabled = True

    def __init__(self, yellow_enough=True, ap_ok=True):
        self.cl1_enough_yellow_coins = yellow_enough
        self.ap_ok = ap_ok
        self.checked = []
        self.delayed = []
        self.stopped = []
        self.config = SimpleNamespace(
            OpsiHazard1Leveling_MinimumActionPointReserve=200,
            opsi_task_delay=lambda **kwargs: self.delayed.append(kwargs),
            task_stop=lambda: self.stopped.append(1),
        )

    def action_point_check(self, preserve):
        self.checked.append(preserve)
        return self.ap_ok


def test_cl1_ap_preserve_uses_the_task_action_point_reserve():
    stub = Cl1ApPreserveStub(ap_ok=False)
    with patch('module.os.map.get_os_reset_remain', return_value=5):
        OSMap.cl1_ap_preserve(stub)
    assert stub.checked == [200]
    assert stub.delayed == [{'cl1_preserve': True}]
    assert stub.stopped == [1]


def test_cl1_ap_preserve_skips_when_coins_are_not_enough():
    stub = Cl1ApPreserveStub(yellow_enough=False)
    with patch('module.os.map.get_os_reset_remain', return_value=5):
        OSMap.cl1_ap_preserve(stub)
    assert stub.checked == []
    assert stub.delayed == []


def test_cl1_ap_preserve_skips_under_smart_scheduling():
    stub = Cl1ApPreserveStub()
    stub.is_smart_scheduling_enabled = True
    with patch('module.os.map.get_os_reset_remain', return_value=5):
        OSMap.cl1_ap_preserve(stub)
    assert stub.checked == []


# ---- 任务层 ActionPointLimit 兜底 ----

class OsRunStub:
    opsi_meowfficer_farming = OSCampaignRun.opsi_meowfficer_farming
    opsi_obscure = OSCampaignRun.opsi_obscure
    opsi_abyssal = OSCampaignRun.opsi_abyssal
    opsi_stronghold = OSCampaignRun.opsi_stronghold

    def __init__(self):
        self.delays = []
        self.calls = []
        self.stopped = []
        self.config = SimpleNamespace(
            is_task_enabled=lambda name: False,
            task_delay=lambda **kwargs: self.delays.append(kwargs),
            task_call=lambda name, **kwargs: self.calls.append(name),
            task_stop=lambda: self.stopped.append(1),
            opsi_task_delay=lambda **kwargs: self.delays.append(kwargs),
        )

    def load_campaign(self):
        raise ActionPointLimit(current=1000, total=1000, preserve=1000)


def test_ap_limit_fallback_no_longer_switches_to_cl1():
    """独立任务行动力不足时只延迟，不再按黄币阈值切侵蚀一。"""
    for method_name in ('opsi_obscure', 'opsi_abyssal', 'opsi_stronghold'):
        stub = OsRunStub()
        getattr(stub, method_name)()
        assert stub.calls == [], method_name
        assert stub.delays == [{'ap_limit': True}], method_name


def test_meow_ap_limit_fallback_only_calls_reward():
    stub = OsRunStub()
    with patch('module.campaign.os_run.get_os_reset_remain', return_value=5):
        stub.opsi_meowfficer_farming()
    assert stub.calls == ['Reward']
    assert {'server_update': True} in stub.delays


# ---- 调度代理层捕获 ActionPointLimit ----

class _StopCounter:
    n = 0


class ProxyStub:
    # 本文件只验证行动力不足的兜底路径，大世界推送关掉（见 test_opsi_notify）
    is_smart_scheduling_enabled = False

    def __init__(self, raise_limit=True):
        self.delayed = []
        self.stop_counter = _StopCounter()
        self.postpones = []
        self.handler_calls = []
        self.config = SimpleNamespace(
            task_delay=lambda **kwargs: self.delayed.append(kwargs),
            task_stop=lambda: setattr(self.stop_counter, 'n', self.stop_counter.n + 1),
        )
        self._raise_limit = raise_limit

    def _get_coin_task_handler(self, task_name):
        def handler(fresh_ap=None, ap_checked=False, ap_preserve=None):
            self.handler_calls.append({
                'fresh_ap': fresh_ap,
                'ap_checked': ap_checked,
                'ap_preserve': ap_preserve,
            })
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
    # 代跑短猫必须带 ap_checked=True：调度决策刚查过行动力，短猫不该再查一遍。
    assert stub.handler_calls == [
        {'fresh_ap': None, 'ap_checked': True, 'ap_preserve': 0}]


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
    assert stub.handler_calls == [
        {'fresh_ap': None, 'ap_checked': True, 'ap_preserve': 0}]


# ---- 塞壬要塞清空后的检查推迟（对齐 AP master：推迟到下次要塞刷新）----

def _make_stronghold_stub(weekly, monthly):
    import module.os.tasks.scheduling as sched_mod

    state = {}

    class StrongholdStub:
        RESET_CHECK_GRACE = sched_mod.OpsiScheduling.RESET_CHECK_GRACE

        def _get_smart_state(self):
            return dict(state)

        def _save_smart_state(self, s):
            state.clear()
            state.update(s)

        _postpone_coin_task_check = OpsiScheduling._postpone_coin_task_check
        _get_coin_task_postpone = OpsiScheduling._get_coin_task_postpone
        _get_next_stronghold_check_time = OpsiScheduling._get_next_stronghold_check_time

    stub = StrongholdStub()
    patches = (sched_mod.get_nearest_weekday_date, sched_mod.get_os_next_reset,
               sched_mod.get_os_reset_remain)
    return stub, state, patches


def test_stronghold_postpone_until_next_refresh():
    import module.os.tasks.scheduling as sched_mod

    # 日期必须相对"现在"取：推迟标记一旦过期就会被读的时候丢掉，
    # 写死日期会让这个用例只在写它的那天成立。
    now = dt.now()
    weekly = dt(now.year, now.month, now.day) + timedelta(days=3)   # 下个要塞刷新点
    monthly = dt(now.year, now.month, now.day) + timedelta(days=26)  # 下次月度重置（更晚）
    stub, state, patches = _make_stronghold_stub(weekly, monthly)
    p_w, p_m, p_r = patches
    saved = (sched_mod.get_nearest_weekday_date, sched_mod.get_os_next_reset,
             sched_mod.get_os_reset_remain)
    sched_mod.get_nearest_weekday_date = lambda target: weekly
    sched_mod.get_os_next_reset = lambda: monthly
    sched_mod.get_os_reset_remain = lambda: 10
    try:
        stub._postpone_coin_task_check('OpsiStronghold', 'cleared')
        stored = stub._get_coin_task_postpone('OpsiStronghold')
        grace = OpsiScheduling.RESET_CHECK_GRACE
        assert stored == weekly + grace          # 周一早于月度重置 → 取周一
        assert stored != get_server_next_update('00:00')   # 不是「延到下次刷新」那条路
    finally:
        (sched_mod.get_nearest_weekday_date, sched_mod.get_os_next_reset,
         sched_mod.get_os_reset_remain) = saved


def test_stronghold_postpone_takes_earlier_of_weekly_and_monthly():
    import module.os.tasks.scheduling as sched_mod

    now = dt.now()
    weekly = dt(now.year, now.month, now.day) + timedelta(days=10)  # 下个要塞刷新点（更晚）
    monthly = dt(now.year, now.month, now.day) + timedelta(days=2)   # 月度重置（更早）
    stub, state, patches = _make_stronghold_stub(weekly, monthly)
    p_w, p_m, p_r = patches
    saved = (sched_mod.get_nearest_weekday_date, sched_mod.get_os_next_reset,
             sched_mod.get_os_reset_remain)
    sched_mod.get_nearest_weekday_date = lambda target: weekly
    sched_mod.get_os_next_reset = lambda: monthly
    sched_mod.get_os_reset_remain = lambda: 10
    try:
        stub._postpone_coin_task_check('OpsiStronghold', 'cleared')
        stored = stub._get_coin_task_postpone('OpsiStronghold')
        assert stored == monthly + OpsiScheduling.RESET_CHECK_GRACE
    finally:
        (sched_mod.get_nearest_weekday_date, sched_mod.get_os_next_reset,
         sched_mod.get_os_reset_remain) = saved


def test_non_stronghold_tasks_keep_daily_postpone():
    """隐秘/深渊的清空推迟不受要塞刷新逻辑影响。"""
    import module.os.tasks.scheduling as sched_mod

    stub, state, patches = _make_stronghold_stub({}, {})
    p_w, p_m, p_r = patches
    saved = (sched_mod.get_os_reset_remain,)
    sched_mod.get_os_reset_remain = lambda: 10
    try:
        stub._postpone_coin_task_check('OpsiObscure', 'cleared')
        stored = stub._get_coin_task_postpone('OpsiObscure')
        assert stored == get_server_next_update('00:00')
    finally:
        sched_mod.get_os_reset_remain = saved[0]
