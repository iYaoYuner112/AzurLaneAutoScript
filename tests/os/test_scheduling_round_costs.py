"""调度循环每轮的开销与门控（对齐 AzurPilot 的省法）。

两件事：
1. 智能调度代跑侵蚀1 时，决策层每轮开头已经读过黄币并按保留线分派过任务，
   侵蚀1 这一轮不再进情报页重读一次（AzurPilot 在调度上下文整段跳过黄币检查）。
2. 计划作战被意外打断时（`run_strategic_search` 返回 False），短猫这一轮不再
   「换 4 支舰队扫雷达」，留给下一轮（AzurPilot 的 `if search_completed:` 门控）；
   侵蚀1 反过来：只告警，照样扫图，因为找事件是它这一轮的正事。
"""

from types import SimpleNamespace

from module.os.tasks.hazard_leveling import OpsiHazard1Leveling
from module.os.tasks.meowfficer_farming import OpsiMeowfficerFarming
from module.os.tasks.task_context import OpsiTaskContext


class TaskEnd(Exception):
    """替 `config.task_stop()` 抛的收口异常。"""


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Cl1RoundStub:
    """跑完整一轮 `os_hazard1_leveling`，把每个界面操作记成一条事件。"""

    _cl1_resource_check = OpsiHazard1Leveling._cl1_resource_check
    os_hazard1_leveling = OpsiHazard1Leveling.os_hazard1_leveling

    def __init__(self, proxy, smart=True, yellow_coins=999999):
        self.is_smart_scheduling_enabled = smart
        self.yellow_coins = yellow_coins
        self.calls = []
        self._action_point_total = 1000
        self.zone = SimpleNamespace(zone_id=44)
        self.is_zone_name_hidden = True
        self.yellow_coins_preserve = 35000
        self.nearest_task_cooling_down = None
        self.config = SimpleNamespace(
            override=lambda **kwargs: None,
            is_task_enabled=lambda name: False,
            cross_get=lambda keys, default=None: default,
            cross_set=lambda **kwargs: None,
            multi_set=lambda: _NullContext(),
            task_delay=lambda **kwargs: None,
            task_call=lambda name: self.calls.append(f'call:{name}'),
            task_stop=lambda: self._raise_stop(),
            check_task_switch=lambda: None,
            OS_ACTION_POINT_PRESERVE=200,
            OpsiScheduling_ActionPointPreserve=200,
            OpsiScheduling_OperationCoinsPreserve=20000,
            OpsiGeneral_BuyActionPointLimit=0,
            OpsiHazard1Leveling_TargetZone=44,
            OpsiFleet_Fleet=1,
        )
        if proxy:
            self.config._opsi_context = OpsiTaskContext(
                parent_task='OpsiScheduling', current_task='OpsiHazard1Leveling')

    def _raise_stop(self):
        self.calls.append('stop')
        raise TaskEnd()

    def get_yellow_coins(self):
        self.calls.append('yellow_coins')
        return self.yellow_coins

    def is_in_opsi_explore(self):
        return False

    def _prepare_scheduling_action_point(self, fresh_ap, cost):
        self.calls.append(f'ap_merge:{cost}')
        return fresh_ap

    def get_current_zone(self):
        self.calls.append('current_zone')

    def action_point_reusable(self, fresh_ap, cost):
        return fresh_ap is not None

    def action_point_set(self, **kwargs):
        self.calls.append('ap_popup')

    def name_to_zone(self, zone_id):
        return SimpleNamespace(zone_id=zone_id)

    def globe_goto(self, *args, **kwargs):
        self.calls.append('globe_goto')

    def fleet_set(self, fleet):
        self.calls.append('fleet_set')

    def run_strategic_search(self):
        self.calls.append('strategic_search')
        return True

    def _forced_move_enabled(self):
        return False

    def handle_after_auto_search(self):
        self.calls.append('after_search')


def run_cl1_round(**kwargs):
    stub = Cl1RoundStub(**kwargs)
    try:
        stub.os_hazard1_leveling(fresh_ap=(1000, 120) if kwargs.get('proxy') else None)
    except TaskEnd:
        pass
    return stub


def test_proxy_round_does_not_read_yellow_coins_again():
    """代跑：决策层读过黄币了，这一轮不再进情报页重读。"""
    stub = run_cl1_round(proxy=True)
    assert 'yellow_coins' not in stub.calls
    # 面板合并、导航、计划作战都照常跑，结束时交回调度
    # （已经在 44 号海域且海域名已隐藏，所以不需要再 goto）
    assert stub.calls == [
        'ap_merge:70', 'current_zone', 'fleet_set',
        'strategic_search', 'after_search',
    ]


def test_standalone_round_still_checks_yellow_coins():
    """独立跑（非代理）：黄币检查不能丢，够了才继续练级。"""
    stub = run_cl1_round(proxy=False, smart=True, yellow_coins=999999)
    assert stub.calls[0] == 'yellow_coins'
    assert 'strategic_search' in stub.calls


def test_standalone_round_with_low_coins_hands_over_to_scheduling():
    """独立跑且黄币不足：交回智能调度并收口，不再去打这一轮。"""
    stub = run_cl1_round(proxy=False, smart=True, yellow_coins=100)
    assert stub.calls == ['yellow_coins', 'call:OpsiScheduling', 'stop']
    assert 'strategic_search' not in stub.calls


def test_non_scheduling_low_coins_delays_and_calls_other_tasks():
    """完全独立的侵蚀1（没开智能调度）：延迟到刷新，并把短猫/深海等叫起来。"""
    stub = Cl1RoundStub(proxy=False, smart=False, yellow_coins=100)
    try:
        stub._cl1_resource_check()
    except TaskEnd:
        pass
    assert 'call:OpsiMeowfficerFarming' in stub.calls
    assert stub.calls[-1] == 'stop'


def test_non_scheduling_enough_coins_returns_without_stopping():
    stub = Cl1RoundStub(proxy=False, smart=False, yellow_coins=999999)
    assert OpsiHazard1Leveling._cl1_resource_check(stub) is None
    assert stub.calls == ['yellow_coins']


class MeowRoundStub:
    """跑短猫的两种计划作战模式，只看搜索被打断时还扫不扫雷达。"""

    _meow_handle_traditional_zone = OpsiMeowfficerFarming._meow_handle_traditional_zone
    _meow_handle_stay_in_zone = OpsiMeowfficerFarming._meow_handle_stay_in_zone

    def __init__(self, search_completed, same_zone=True):
        self.calls = []
        self.search_completed = search_completed
        self.zone = SimpleNamespace(zone_id=44 if same_zone else 22)
        self.is_zone_name_hidden = True
        self.config = SimpleNamespace(
            OpsiFleet_Fleet=1,
            OpsiFleet_Submarine=False,
            check_task_switch=lambda: None)

    def globe_goto(self, *args, **kwargs):
        self.calls.append('globe_goto')

    def fleet_set(self, fleet):
        self.calls.append('fleet_set')

    def os_order_execute(self, **kwargs):
        self.calls.append('order')

    def get_current_zone(self):
        self.calls.append('current_zone')

    def action_point_reusable(self, fresh_ap, cost):
        return fresh_ap is not None

    def action_point_set(self, **kwargs):
        self.calls.append('ap_popup')

    def run_strategic_search(self):
        self.calls.append('strategic_search')
        return self.search_completed

    def _meow_fixed_patrol_scan(self):
        self.calls.append('radar_patrol')

    def handle_after_auto_search(self):
        self.calls.append('after_search')


def run_meow(mode, search_completed, **kwargs):
    stub = MeowRoundStub(search_completed=search_completed, **kwargs)
    zone = SimpleNamespace(zone_id=44)
    if mode == 'traditional':
        stub._meow_handle_traditional_zone(zone)
    else:
        stub._meow_handle_stay_in_zone(zone, fresh_ap=(1000, 142))
    return stub


def test_traditional_zone_runs_radar_patrol_when_search_completed():
    stub = run_meow('traditional', True)
    assert stub.calls[-2:] == ['radar_patrol', 'after_search']


def test_traditional_zone_skips_radar_patrol_when_interrupted():
    """搜索被打断 -> 画面状态不可信，换 4 支队扫雷达留给下一轮，但收尾照做。"""
    stub = run_meow('traditional', False)
    assert 'radar_patrol' not in stub.calls
    assert stub.calls[-1] == 'after_search'
    assert 'strategic_search' in stub.calls


def test_stay_in_zone_skips_radar_patrol_when_interrupted():
    stub = run_meow('stay', False)
    assert 'radar_patrol' not in stub.calls
    assert stub.calls[-1] == 'after_search'


def test_stay_in_zone_runs_radar_patrol_when_search_completed():
    stub = run_meow('stay', True)
    assert stub.calls[-2:] == ['radar_patrol', 'after_search']


def test_stay_in_zone_reopens_popup_after_changing_zone():
    """换海域会消耗行动力，决策首读不能复用，必须重开行动点弹窗。"""
    stub = run_meow('stay', True, same_zone=False)
    assert 'ap_popup' in stub.calls
    assert stub.calls.index('globe_goto') < stub.calls.index('ap_popup')
