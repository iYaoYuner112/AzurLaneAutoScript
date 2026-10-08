from datetime import datetime
from types import SimpleNamespace

from module.os.map import OSMap, should_move_fleet_for_fixed_patrol
from module.os.tasks.scheduling import OpsiScheduling, decide_resource_action
from module.statistics.resource_monitor import record_dashboard_resource


def decide(yellow_coins, total_ap, active=False, coin_target_mode=True, ap_active=False):
    return decide_resource_action(
        yellow_coins=yellow_coins,
        total_ap=total_ap,
        coin_preserve=20000,
        coin_return_threshold=60000,
        ap_preserve=200,
        meow_ap_preserve=1000,
        coin_target_mode=coin_target_mode,
        coin_replenish_active=active,
        ap_replenish_active=ap_active,
    )


def test_low_coins_start_meowfficer_replenishment():
    assert decide(19999, 1001) == ('meow', True, False)


def test_active_replenishment_continues_until_total_reaches_80000():
    assert decide(79999, 1001, active=True) == ('meow', True, False)
    assert decide(80000, 1001, active=True) == ('cl1', False, False)


def test_coin_target_mode_does_not_replenish_before_threshold():
    assert decide(20000, 1001) == ('cl1', False, False)


def test_insufficient_ap_waits_without_clearing_replenishment_state():
    assert decide(30000, 200, active=True) == ('wait', True, False)
    assert decide(30000, 1000, active=True) == ('wait', True, False)
    assert decide(30000, 1001, active=True) == ('meow', True, False)


def test_action_point_mode_replenishes_only_below_coin_reserve():
    assert decide(19999, 1001, coin_target_mode=False) == ('meow', False, True)
    assert decide(20000, 1001, coin_target_mode=False) == ('cl1', False, False)
    assert decide(19999, 1000, coin_target_mode=False) == ('wait', False, False)


def test_action_point_mode_hysteresis_keeps_replenishing():
    # 黄币已涨回保留值之上，但 ap_replenish_active 仍置位 → 继续补（迟滞）。
    assert decide(21000, 1001, coin_target_mode=False, ap_active=True) == ('meow', False, True)
    # 行动力降到阈值才清位，黄币仍够 → 恢复侵蚀1。
    assert decide(21000, 1000, coin_target_mode=False, ap_active=True) == ('cl1', False, False)


def decide_at_coin_line(gate, yellow_coins, total_ap, active=False):
    """按给定的补黄币开工线跑一次决策，其余口径同默认配置。"""
    return decide_resource_action(
        yellow_coins=yellow_coins,
        total_ap=total_ap,
        coin_preserve=20000,
        coin_return_threshold=60000,
        ap_preserve=200,
        meow_ap_preserve=gate,
        coin_target_mode=True,
        coin_replenish_active=active,
        ap_replenish_active=False,
    )


def test_ap_band_between_the_two_lines_now_replenishes_coins():
    """600 总行动力 + 黄币不足：旧口径（线画在 1000）只会空等到明天。"""
    assert decide_at_coin_line(200, 19999, 600) == ('meow', True, False)
    assert decide_at_coin_line(1000, 19999, 600) == ('wait', True, False)
    # 降到自己的线上才停，不会把行动力吃穿。
    assert decide_at_coin_line(200, 19999, 200, active=True) == ('wait', True, False)


class CoinGateStub:
    """只提供补黄币开工线计算所需的 config。

    两个计算函数直接借用生产实现：这条线由「调度保留线 → 回退短猫保留线」两步
    组成，只测其中一步会漏掉它们之间的衔接。AzurPilot 不封顶也不做月末降线。
    """

    _get_coin_task_action_point_preserve = OpsiScheduling._get_coin_task_action_point_preserve
    _get_scheduled_meow_ap_preserve = OpsiScheduling._get_scheduled_meow_ap_preserve

    def __init__(self, ap_preserve, meow_preserve):
        self.config = SimpleNamespace(
            OpsiScheduling_ActionPointPreserve=ap_preserve,
            OpsiScheduling_MeowfficerActionPointPreserve=meow_preserve,
        )


def test_coin_task_start_line_follows_the_scheduler_reserve():
    assert OpsiScheduling._get_coin_task_action_point_preserve(CoinGateStub(200, 1000)) == 200


def test_coin_task_start_line_falls_back_to_the_meowfficer_reserve():
    assert OpsiScheduling._get_coin_task_action_point_preserve(CoinGateStub(0, 1000)) == 1000


def test_coin_task_start_line_is_used_as_configured():
    # 对齐 AzurPilot：线画多高就用多高，不截到 2000。
    assert OpsiScheduling._get_scheduled_meow_ap_preserve(CoinGateStub(100000, 1000)) == 100000
    # 也没有月末降线那一层，配置值原样传下去。
    assert OpsiScheduling._get_scheduled_meow_ap_preserve(CoinGateStub(200, 1000)) == 200
    assert OpsiScheduling._get_scheduled_meow_ap_preserve(CoinGateStub(0, 0)) == 0


class FakeResourceConfig:
    def __init__(self):
        self.resources = {}
        self.modified = {}

    def cross_get(self, keys, default=None):
        return self.resources.get('ResourceMonitor', default)

    def cross_set(self, keys, value):
        self.resources['ResourceMonitor'] = value

    def save(self):
        self.resources['ResourceMonitor'] = self.modified['Alas.Storage.Storage.ResourceMonitor']
        self.modified.clear()


def test_resource_monitor_records_value_and_action_point_total():
    config = FakeResourceConfig()

    recorded = record_dashboard_resource(
        config,
        'ActionPoint',
        value=125,
        total=1125,
        now=__import__('datetime').datetime(2026, 10, 1, 12, 0, 0),
    )

    assert recorded
    assert config.resources['ResourceMonitor']['ActionPoint'] == {
        'Value': 125,
        'Total': 1125,
        'Record': '2026-10-01 12:00:00',
    }


def test_child_task_keeps_identity_and_owner_is_restored():
    """代理子任务时：子任务看到自己的身份，拥有者仍是调度器，异常后全部还原。"""
    from module.config.config import Function
    from module.os.tasks.task_context import is_running_opsi_proxy, opsi_task_context

    def make_function(command):
        return Function({
            'Scheduler': {
                'Command': command,
                'Enable': True,
                'NextRun': datetime(2026, 1, 1),
            }
        })

    class FakeConfig:
        def __init__(self):
            self.task = make_function('OpsiScheduling')
            self.data = {'OpsiHazard1Leveling': {
                'Scheduler': {
                    'Command': 'OpsiHazard1Leveling',
                    'Enable': True,
                    'NextRun': datetime(2026, 1, 1),
                }
            }}
            self.bindings = []
            self.child_identity = None
            self.owner_identity = None
            self.proxy_running = None

        def bind(self, task, func_list=None):
            self.bindings.append((task, tuple(func_list or ())))

    config = FakeConfig()
    owner = config.task

    def run_child():
        config.child_identity = config.task.command
        config.owner_identity = getattr(config._task_switch_owner, 'command', None)
        config.proxy_running = is_running_opsi_proxy(config)
        raise RuntimeError('child task failed')

    try:
        with opsi_task_context(config, 'OpsiHazard1Leveling'):
            run_child()
    except RuntimeError:
        pass
    else:
        raise AssertionError('expected child task failure')

    # The child keeps its own identity; the scheduler stays the owner.
    assert config.child_identity == 'OpsiHazard1Leveling'
    assert config.owner_identity == 'OpsiScheduling'
    assert config.proxy_running is True
    # Binding and temporary attributes are restored after the failure.
    assert config.bindings[-1] == ('OpsiScheduling', ())
    assert config.task is owner
    assert not hasattr(config, '_opsi_context')
    assert not hasattr(config, '_task_switch_owner')
    assert not hasattr(config, '_disable_task_switch')


def test_fixed_patrol_uses_threshold_above_seven_ap():
    assert OSMap._FIXED_PATROL_L2_AP == 7
    assert not should_move_fleet_for_fixed_patrol(7, False)
    assert should_move_fleet_for_fixed_patrol(8, False)
    assert should_move_fleet_for_fixed_patrol(0, True)