from module.os.tasks.scheduling import decide_resource_action


def decide(yellow_coins, total_ap, active=False, coin_target_mode=True):
    return decide_resource_action(
        yellow_coins=yellow_coins,
        total_ap=total_ap,
        coin_preserve=20000,
        coin_return_threshold=60000,
        ap_preserve=200,
        coin_target_mode=coin_target_mode,
        coin_replenish_active=active,
    )


def test_low_coins_start_meowfficer_replenishment():
    assert decide(19999, 1000) == ('meow', True)


def test_active_replenishment_continues_until_total_reaches_80000():
    assert decide(79999, 1000, active=True) == ('meow', True)
    assert decide(80000, 1000, active=True) == ('cl1', False)


def test_coin_target_mode_does_not_replenish_before_threshold():
    assert decide(20000, 1000) == ('cl1', False)


def test_insufficient_ap_waits_without_clearing_replenishment_state():
    assert decide(30000, 200, active=True) == ('wait', True)


def test_action_point_mode_replenishes_only_below_coin_reserve():
    assert decide(19999, 1000, coin_target_mode=False) == ('meow', False)
    assert decide(20000, 1000, coin_target_mode=False) == ('cl1', False)