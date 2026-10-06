"""大世界推送的行为约束：频率、去重、开关与失败处理。

推送一律走 `module.notify.handle_notify`，测试里把它换成记录器，
这样既能断言「推了几条、推了什么」，也不会真的连网络。
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import module.notify as notify_module
from module.config.config import TaskEnd
from module.os.opsi_notify import (
    AP_NOTIFY_MIN_INTERVAL_MINUTES,
    RUNTIME_ATTR_ACTION_POINT,
    RUNTIME_ATTR_ACTION_POINT_LOW,
    RUNTIME_ATTR_COIN_TASK,
    STATE_KEY_LAST_ACTION_POINT,
    clear_coin_task_push_state,
    notify_action_point_change,
    notify_ap_insufficient,
    notify_coin_task_disabled,
    notify_coin_task_proxy,
    notify_coins_ap_insufficient,
)
from module.os.tasks.scheduling import OpsiScheduling
from module.os.tasks.task_context import OpsiStatus

PUSH_CHANNEL = 'provider: wechatworkbot\nkey: secret'


class StubConfig:
    """真实配置里推送相关的那几个字段，加上调度状态字典的读写。"""

    def __init__(self, push_config=PUSH_CHANNEL, notify_mail=True):
        self.config_name = 'Test'
        self.Error_OnePushConfig = push_config
        self.OpsiGeneral_NotifyOpsiMail = notify_mail
        self.OS_ACTION_POINT_BOX_USE = False
        self.OpsiScheduling_ActionPointPreserve = 200
        self.OpsiScheduling_MeowfficerActionPointPreserve = 0
        self.OpsiScheduling_OperationCoinsPreserve = 20000
        self.OpsiScheduling_OperationCoinsReturnThreshold = 60000
        self.OpsiScheduling_UseSmartSchedulingOperationCoinsPreserve = True
        self.values = {}
        self.delays = []

    def cross_get(self, keys, default=None):
        return self.values.get(keys, default)

    def cross_set(self, keys, value):
        self.values[keys] = value

    def task_delay(self, **kwargs):
        self.delays.append(kwargs)

    @staticmethod
    def task_stop():
        raise TaskEnd


class NotifyStub:
    """只提供推送模块用到的接口：config、调度状态字典、行动力读数。"""

    def __init__(self, ap=1500, push_config=PUSH_CHANNEL, notify_mail=True, scheduling=True):
        self.state = {}
        self._action_point_total = ap
        self.is_smart_scheduling_enabled = scheduling
        self.config = StubConfig(push_config=push_config, notify_mail=notify_mail)

    def _get_smart_state(self):
        return dict(self.state)

    def _save_smart_state(self, state):
        self.state = dict(state)

    def set_ap(self, ap):
        self._action_point_total = ap


def bind(stub, *names):
    """把真实的 OpsiScheduling 方法接到替身上，用来验证钩子位置。"""
    for name in names:
        def call(*args, method=name, **kwargs):
            return getattr(OpsiScheduling, method)(stub, *args, **kwargs)
        setattr(stub, name, call)
    return stub


class Recorder:
    def __init__(self, ok=True, error=None):
        self.ok = ok
        self.error = error
        self.calls = []

    def __call__(self, config, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.ok

    @property
    def titles(self):
        return [call['title'] for call in self.calls]


@contextmanager
def push_recorder(ok=True, error=None):
    original = notify_module.handle_notify
    recorder = Recorder(ok=ok, error=error)
    notify_module.handle_notify = recorder
    try:
        yield recorder
    finally:
        notify_module.handle_notify = original


def expire_push_window(stub):
    """把挂在 config 上的推送时间戳推到窗口之外，模拟过了 30 分钟。"""
    past = datetime.now() - timedelta(minutes=AP_NOTIFY_MIN_INTERVAL_MINUTES + 1)
    for key, value in vars(stub.config).items():
        if not key.startswith('opsi_notify_'):
            continue
        if isinstance(value, datetime):
            setattr(stub.config, key, past)
        elif isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], datetime):
            setattr(stub.config, key, (value[0], past))


def resume_after_task_boundary(stub, ap):
    """换一个 Config 对象：内存里的推送窗口清零，落盘的调度状态原样带过去。

    `Alas.run_loop` 每跑完一个任务就 `del_cached_property(self, 'config')`，
    重启进程也是同样效果，所以窗口只在单次任务运行内累计。
    """
    revived = NotifyStub(
        ap=ap,
        push_config=stub.config.Error_OnePushConfig,
        notify_mail=stub.config.OpsiGeneral_NotifyOpsiMail,
    )
    revived.state = dict(stub.state)
    return revived


def test_no_channel_configured_pushes_nothing():
    stub = NotifyStub(push_config='provider: null')
    with push_recorder() as recorder:
        assert notify_action_point_change(stub) is False
        assert notify_ap_insufficient(stub, 100, 200) is False
        assert notify_coin_task_disabled(stub) is False
    assert recorder.calls == []


def test_switch_off_stops_world_pushes():
    stub = NotifyStub(notify_mail=False)
    with push_recorder() as recorder:
        assert notify_action_point_change(stub) is False
    assert recorder.calls == []


def test_pushes_only_when_smart_scheduling_enabled():
    stub = NotifyStub(scheduling=False)
    with push_recorder() as recorder:
        assert notify_action_point_change(stub) is False
        assert notify_coins_ap_insufficient(stub, 100, 100, 20000, 1000) is False
    assert recorder.calls == []


def test_first_action_point_reading_is_pushed():
    stub = NotifyStub(ap=1500)
    with push_recorder() as recorder:
        assert notify_action_point_change(stub) is True
    assert recorder.titles == ['Alas <Test> 行动力出现变化！']
    assert recorder.calls[0]['content'] == '总行动力: 1500'
    assert stub.state[STATE_KEY_LAST_ACTION_POINT] == 1500


def test_unchanged_action_point_is_not_pushed():
    stub = NotifyStub(ap=1500)
    with push_recorder() as recorder:
        assert notify_action_point_change(stub) is True
        expire_push_window(stub)
        assert notify_action_point_change(stub) is False
    assert len(recorder.calls) == 1


def test_action_point_delta_is_reported():
    stub = NotifyStub(ap=1500)
    with push_recorder() as recorder:
        notify_action_point_change(stub)
        expire_push_window(stub)
        stub.set_ap(1620)
        assert notify_action_point_change(stub) is True
        expire_push_window(stub)
        stub.set_ap(1420)
        assert notify_action_point_change(stub) is True
    assert recorder.calls[1]['content'] == '总行动力: 1620 上涨120行动力'
    # 对比的是上次推送出去的值（1620），不是最初那次读到的 1500。
    assert recorder.calls[2]['content'] == '总行动力: 1420 下跌200行动力'


def test_task_boundary_resets_the_push_window():
    """窗口挂在 config 上：任务边界换了 Config，同类消息立刻可以再推（照 AP）。"""
    stub = NotifyStub(ap=1500)
    with push_recorder() as recorder:
        assert notify_action_point_change(stub) is True
        next_run = resume_after_task_boundary(stub, ap=1620)
        assert notify_action_point_change(next_run) is True
    assert len(recorder.calls) == 2
    assert recorder.calls[1]['content'] == '总行动力: 1620 上涨120行动力'


def test_restart_does_not_repush_unchanged_action_point():
    """行动力值落在 storage：任务边界清零了窗口，值没变依然不重复推。"""
    stub = NotifyStub(ap=1500)
    with push_recorder() as recorder:
        assert notify_action_point_change(stub) is True
        next_run = resume_after_task_boundary(stub, ap=1500)
        assert notify_action_point_change(next_run) is False
    assert len(recorder.calls) == 1


def test_action_point_change_waits_for_the_window():
    stub = NotifyStub(ap=1500)
    with push_recorder() as recorder:
        assert notify_action_point_change(stub) is True
        stub.set_ap(1600)
        assert notify_action_point_change(stub) is False
    assert len(recorder.calls) == 1
    assert stub.state[STATE_KEY_LAST_ACTION_POINT] == 1500


def test_ap_insufficient_pushes_once_per_window():
    stub = NotifyStub()
    with push_recorder() as recorder:
        assert notify_ap_insufficient(stub, 180, 200) is True
        assert notify_ap_insufficient(stub, 181, 200) is False
        expire_push_window(stub)
        assert notify_ap_insufficient(stub, 181, 200) is True
    assert recorder.titles == ['Alas <Test> 智能调度- 行动力不足'] * 2
    assert recorder.calls[0]['content'] == '总行动力 180 低于最低保留 200，推迟任务'


def test_coins_and_ap_shortage_is_a_separate_message():
    stub = NotifyStub()
    with push_recorder() as recorder:
        assert notify_coins_ap_insufficient(stub, 19000, 900, 20000, 1000) is True
    content = recorder.calls[0]['content']
    assert recorder.titles == ['Alas <Test> 智能调度- 黄币与行动力双重不足']
    assert '黄币: 19000，补黄币阈值: 20000' in content
    assert '总行动力 900 不足 (需要 1000)' in content


def test_failed_push_is_not_marked_as_sent():
    stub = NotifyStub()
    with push_recorder(ok=False) as recorder:
        assert notify_ap_insufficient(stub, 180, 200) is False
        assert notify_ap_insufficient(stub, 180, 200) is False
    # 尝试时刻已经写入，渠道故障期间不会每轮重试；成功时间戳则没有记录。
    assert len(recorder.calls) == 1
    assert isinstance(
        getattr(stub.config, f'{RUNTIME_ATTR_ACTION_POINT_LOW}_attempt', None), datetime)
    assert getattr(stub.config, RUNTIME_ATTR_ACTION_POINT_LOW, None) is None


def test_channel_exception_does_not_break_the_task():
    stub = NotifyStub()
    with push_recorder(error=RuntimeError('boom')) as recorder:
        assert notify_action_point_change(stub) is False
    assert len(recorder.calls) == 1
    assert STATE_KEY_LAST_ACTION_POINT not in stub.state


def test_same_coin_task_proxies_push_once():
    stub = NotifyStub()
    with push_recorder() as recorder:
        assert notify_coin_task_proxy(stub, 19000, 1500, 20000, 1000, 'OpsiMeowfficerFarming') is True
        assert notify_coin_task_proxy(stub, 18000, 1400, 20000, 1000, 'OpsiMeowfficerFarming') is False
        assert notify_coin_task_proxy(stub, 17000, 1300, 20000, 1000, 'OpsiObscure') is True
    assert len(recorder.calls) == 2
    assert stub.config.opsi_notify_coin_task == 'OpsiObscure'
    assert '已代理执行一轮隐秘海域获取黄币' in recorder.calls[1]['content']


def test_coin_task_push_resumes_after_cl1():
    stub = NotifyStub()
    with push_recorder() as recorder:
        notify_coin_task_proxy(stub, 19000, 1500, 20000, 1000, 'OpsiMeowfficerFarming')
        expire_push_window(stub)
        assert notify_coin_task_proxy(stub, 18000, 1500, 20000, 1000, 'OpsiMeowfficerFarming') is False
        clear_coin_task_push_state(stub)
        assert notify_coin_task_proxy(stub, 18000, 1500, 20000, 1000, 'OpsiMeowfficerFarming') is True
    assert len(recorder.calls) == 2


def test_no_coin_task_enabled_pushes_every_round():
    # 这种配置错误会让调度推迟到服务器刷新，一天最多一次，所以不设推送窗口。
    stub = NotifyStub()
    with push_recorder() as recorder:
        assert notify_coin_task_disabled(stub) is True
        assert notify_coin_task_disabled(stub) is True
    assert len(recorder.calls) == 2
    assert '请至少启用耄耋相接' in recorder.calls[0]['content']


# ---- 调度层钩子：证明推送真的挂在决策路径上 ----

def make_decision_stub():
    stub = NotifyStub(ap=0)
    stub.get_action_point_limit = lambda: 2000
    bind(
        stub,
        '_run_opsi_scheduler_decision', '_get_scheduled_meow_ap_preserve',
        '_get_coin_task_action_point_preserve', '_get_coin_replenish_target',
        '_get_smart_state', '_save_smart_state', '_sync_scheduling_mode',
        '_decide_scheduling_mode', '_delay_to_server_update',
    )
    return stub


def run_decision(stub, yellow_coins, total_ap):
    try:
        stub._run_opsi_scheduler_decision(yellow_coins, total_ap, total_ap)
    except TaskEnd:
        pass


def test_scheduling_action_point_read_pushes_the_change():
    stub = NotifyStub(ap=0)
    bind(stub, '_close_scheduling_action_point', '_get_scheduling_action_point')

    def safe_get():
        # 模拟行动力弹窗读到的新读数
        stub._action_point_total = 1620
        stub._action_point_current = 20

    stub.action_point_enter = lambda: None
    stub.action_point_quit = lambda: None
    stub.action_point_safe_get = safe_get

    with push_recorder() as recorder:
        assert stub._get_scheduling_action_point() == (1620, 20)
    assert recorder.titles == ['Alas <Test> 行动力出现变化！']
    assert stub.state[STATE_KEY_LAST_ACTION_POINT] == 1620


def test_dispatch_hook_pushes_the_proxied_task():
    stub = NotifyStub(ap=1500)
    bind(
        stub,
        '_dispatch_coin_task', '_get_enabled_coin_tasks', '_get_task_priority',
        '_is_coin_task_enabled', '_get_coin_replenish_target', '_get_coin_task_postpone',
    )
    stub._run_scheduled_coin_task_once = lambda name, ap_preserve, fresh_ap=None: SimpleNamespace(
        executed=True, status=OpsiStatus.SUCCESS, task=name)

    with push_recorder() as recorder:
        result = stub._dispatch_coin_task(19000, 1500, 1000, 1000)
    assert result.task == 'OpsiStronghold'
    assert recorder.titles == ['Alas <Test> 智能调度- 已代理执行黄币补充任务']
    assert '已代理执行一轮塞壬要塞获取黄币' in recorder.calls[0]['content']
    assert getattr(stub.config, RUNTIME_ATTR_COIN_TASK, None) == 'OpsiStronghold'


def test_wait_branch_pushes_coins_and_action_point_shortage():
    stub = make_decision_stub()
    with push_recorder() as recorder:
        # 黄币 19000 低于保留值 20000，行动力 150 又低于补黄币开工线 200
        run_decision(stub, 19000, 150)
    assert recorder.titles == ['Alas <Test> 智能调度- 黄币与行动力双重不足']
    content = recorder.calls[0]['content']
    assert '黄币: 19000，补黄币阈值: 80000' in content
    assert '总行动力 150 不足 (需要 200)' in content


def test_wait_branch_pushes_plain_action_point_shortage():
    stub = make_decision_stub()
    with push_recorder() as recorder:
        # 黄币充足、只撞在行动力保留线 200 上
        run_decision(stub, 90000, 150)
    assert recorder.titles == ['Alas <Test> 智能调度- 行动力不足']
    assert recorder.calls[0]['content'] == '总行动力 150 低于最低保留 200，推迟任务'
