from types import SimpleNamespace

from module.os_handler.os_status import OSStatus


class FakeStatus:
    def __init__(self, preserve, target):
        self.config = SimpleNamespace(
            OpsiHazard1Leveling_YellowCoinsPreserve=preserve,
            OpsiMeowfficerFarming_YellowCoinsTarget=target,
        )

    @property
    def cl1_yellow_coins_preserve(self):
        return OSStatus.cl1_yellow_coins_preserve.fget(self)

    @property
    def cl1_yellow_coins_target(self):
        return OSStatus.cl1_yellow_coins_target.fget(self)


def test_cl1_coin_thresholds_use_configured_values():
    status = FakeStatus(preserve=20000, target=80000)

    assert OSStatus.cl1_yellow_coins_preserve.fget(status) == 20000
    assert OSStatus.cl1_yellow_coins_target.fget(status) == 80000
    assert not OSStatus.is_cl1_yellow_coins_target_reached(status, 79999)
    assert OSStatus.is_cl1_yellow_coins_target_reached(status, 80000)


def test_meowfficer_target_cannot_be_below_cl1_switch_threshold():
    status = FakeStatus(preserve=20000, target=10000)

    assert OSStatus.cl1_yellow_coins_target.fget(status) == 20000
    assert not OSStatus.is_cl1_yellow_coins_target_reached(status, 19999)
    assert OSStatus.is_cl1_yellow_coins_target_reached(status, 20000)