"""短猫三模式分派（对齐 AP master 的顺序）与 StayInZone。

AP master 的 run_meowfficer_farming_once 按这个顺序选模式：
1. 指定海域 + StayInZone 关 → 传统单一指定海域（战略搜索）
2. 指定海域 + StayInZone 开 → 停留该海域循环搜索
3. 没指定海域 → 随机海域搜索
StayInZone 开着但指定海域为 0 时，回退到随机海域搜索（并告警）。
"""

from types import SimpleNamespace

from module.os.tasks.meowfficer_farming import OpsiMeowfficerFarming


class DispatchStub:
    def __init__(self, target_zone, stay):
        self.config = SimpleNamespace(
            OpsiMeowfficerFarming_TargetZone=target_zone,
            OpsiMeowfficerFarming_StayInZone=stay)
        self.calls = []

    def name_to_zone(self, value):
        return SimpleNamespace(zone_id=value)

    def _meow_handle_traditional_zone(self, zone):
        self.calls.append(('traditional', zone.zone_id))

    def _meow_handle_stay_in_zone(self, zone, fresh_ap=None):
        self.calls.append(('stay', zone.zone_id, fresh_ap))

    def _meow_handle_normal_search(self):
        self.calls.append(('normal', None))

    _meow_dispatch = OpsiMeowfficerFarming._meow_dispatch


def test_target_zone_without_stay_uses_traditional():
    stub = DispatchStub(target_zone=44, stay=False)
    stub._meow_dispatch()
    assert stub.calls == [('traditional', 44)]


def test_target_zone_with_stay_keeps_searching_the_zone():
    stub = DispatchStub(target_zone=44, stay=True)
    stub._meow_dispatch()
    assert stub.calls == [('stay', 44, None)]


def test_stay_in_zone_receives_reusable_reading():
    """决策首读要透传给 StayInZone 分支，否则复用无从谈起。"""
    stub = DispatchStub(target_zone=44, stay=True)
    stub._meow_dispatch(fresh_ap=(1695, 142))
    assert stub.calls == [('stay', 44, (1695, 142))]


def test_traditional_mode_ignores_reusable_reading():
    """传统单一海域模式每轮都要重新进海域，读数没有复用价值。"""
    stub = DispatchStub(target_zone=44, stay=False)
    stub._meow_dispatch(fresh_ap=(1695, 142))
    assert stub.calls == [('traditional', 44)]


def test_no_target_zone_uses_random_zone_search():
    stub = DispatchStub(target_zone=0, stay=False)
    stub._meow_dispatch()
    assert stub.calls == [('normal', None)]


def test_stay_without_target_zone_falls_back_to_random_search():
    stub = DispatchStub(target_zone=0, stay=True)
    stub._meow_dispatch()
    assert stub.calls == [('normal', None)]
