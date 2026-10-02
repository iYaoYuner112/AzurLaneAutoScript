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


def test_child_task_settings_are_bound_and_scheduler_binding_is_restored():
    class FakeConfig:
        def __init__(self):
            self.task = SimpleNamespace(command='OpsiScheduling')
            self.bindings = []

        def bind(self, task, func_list=None):
            self.bindings.append((task, tuple(func_list or ())))

    scheduler = SimpleNamespace(config=FakeConfig())

    def run_child():
        assert scheduler.config.bindings[-1] == (
            'OpsiScheduling', ('OpsiHazard1Leveling',)
        )
        assert scheduler.config.task.command == 'OpsiScheduling'
        raise RuntimeError('child task failed')

    try:
        OpsiScheduling._run_with_child_config(
            scheduler, 'OpsiHazard1Leveling', run_child
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError('expected child task failure')

    assert scheduler.config.bindings[-1] == ('OpsiScheduling', ())


def test_fixed_patrol_uses_threshold_above_seven_ap():
    assert OSMap._FIXED_PATROL_L2_AP == 7
    assert not should_move_fleet_for_fixed_patrol(7, False)
    assert should_move_fleet_for_fixed_patrol(8, False)
    assert should_move_fleet_for_fixed_patrol(0, True)