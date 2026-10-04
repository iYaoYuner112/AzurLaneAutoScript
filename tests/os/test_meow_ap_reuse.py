"""短猫行动力弹窗复用（对齐 AzurPilot 的 run_meowfficer_farming_once）。

智能调度代跑短猫时的三步走：

1. 决策读一次行动力，面板**暂留不关**；
2. `ap_checked=True` 让短猫跳过自己那次重复的前置检查
   （否则一轮里多一组 REMAIN_OS + CANCEL）；
3. 已在目标安全海域续跑时，在同一个面板里完成 120 开工补充并复用读数，
   跳过进海域的行动点弹窗。

这里锁定第 3 步的两个决策点：`_meow_prepare_action_point` 什么时候敢用那个面板、
`_meow_handle_stay_in_zone` 什么时候敢跳过弹窗。
"""

import unittest
from types import SimpleNamespace

from module.os.tasks.meowfficer_farming import OpsiMeowfficerFarming


class PrepareStub:
    """替换界面交互，只保留 _meow_prepare_action_point 的分支。"""

    def __init__(self, panel_open=True, stay_in_zone=True, target_zone=12,
                 current_zone=12, name_hidden=True, prepared=(1695, 142)):
        self._scheduling_ap_panel_open = panel_open
        self.config = SimpleNamespace(
            OpsiMeowfficerFarming_StayInZone=stay_in_zone,
            OpsiMeowfficerFarming_TargetZone=target_zone)
        self.is_zone_name_hidden = name_hidden
        if current_zone is not None:
            self.zone = SimpleNamespace(zone_id=current_zone)
        self._prepared = prepared
        self.close_calls = 0
        self.prepare_calls = []

    def _close_scheduling_action_point(self):
        self.close_calls += 1
        self._scheduling_ap_panel_open = False

    def _prepare_scheduling_action_point(self, fresh_ap, *, cost):
        self.prepare_calls.append((fresh_ap, cost))
        return self._prepared


class TestMeowPrepareActionPoint(unittest.TestCase):
    def test_without_retained_panel_never_reuses(self):
        """面板没被保留 → 读数没有新鲜来源，必须返回 None。"""
        stub = PrepareStub(panel_open=False)
        result = OpsiMeowfficerFarming._meow_prepare_action_point(stub, (1695, 142))
        self.assertIsNone(result)
        self.assertEqual(stub.prepare_calls, [])
        self.assertEqual(stub.close_calls, 0)

    def test_same_zone_tops_up_in_retained_panel(self):
        """已在目标安全海域 → 在同一个面板里补到开工线 120。"""
        stub = PrepareStub()
        result = OpsiMeowfficerFarming._meow_prepare_action_point(stub, (1695, 142))
        self.assertEqual(result, (1695, 142))
        self.assertEqual(stub.prepare_calls, [((1695, 142), 120)])
        self.assertEqual(stub.close_calls, 0)

    def test_other_zone_closes_panel(self):
        """船不在目标海域 → 待会要 globe_goto，读数会作废，先关窗。"""
        stub = PrepareStub(current_zone=44)
        self.assertIsNone(OpsiMeowfficerFarming._meow_prepare_action_point(stub, (1695, 142)))
        self.assertEqual(stub.prepare_calls, [])
        self.assertEqual(stub.close_calls, 1)
        self.assertFalse(stub._scheduling_ap_panel_open)

    def test_not_safe_zone_closes_panel(self):
        """海域名没读出「安全海域」→ 位置不确定，不能赌。"""
        stub = PrepareStub(name_hidden=False)
        self.assertIsNone(OpsiMeowfficerFarming._meow_prepare_action_point(stub, (1695, 142)))
        self.assertEqual(stub.prepare_calls, [])
        self.assertEqual(stub.close_calls, 1)

    def test_traditional_mode_closes_panel(self):
        """传统单一海域模式每轮都要重新进海域，读数没有复用价值。"""
        stub = PrepareStub(stay_in_zone=False)
        self.assertIsNone(OpsiMeowfficerFarming._meow_prepare_action_point(stub, (1695, 142)))
        self.assertEqual(stub.prepare_calls, [])
        self.assertEqual(stub.close_calls, 1)

    def test_stay_without_target_zone_closes_panel(self):
        """StayInZone 开着但没指定海域 → 回退随机搜索，先关窗。"""
        stub = PrepareStub(target_zone=0)
        self.assertIsNone(OpsiMeowfficerFarming._meow_prepare_action_point(stub, (1695, 142)))
        self.assertEqual(stub.close_calls, 1)

    def test_missing_zone_object_closes_panel(self):
        """还没读过海域名（self.zone 未赋值）→ 关窗走原流程，不能抛异常。"""
        stub = PrepareStub(current_zone=None)
        self.assertIsNone(OpsiMeowfficerFarming._meow_prepare_action_point(stub, (1695, 142)))
        self.assertEqual(stub.close_calls, 1)


class StayStub:
    """替换 _meow_handle_stay_in_zone 里的界面交互。"""

    def __init__(self, current_zone, name_hidden=True, reusable=True):
        self.zone = SimpleNamespace(zone_id=current_zone)
        self.is_zone_name_hidden = name_hidden
        self.config = SimpleNamespace(
            OpsiFleet_Fleet=1,
            OpsiFleet_Submarine=False,
            check_task_switch=lambda: None)
        self._reusable = reusable
        self.globe_goto_calls = 0
        self.action_point_set_calls = []

    def get_current_zone(self):
        return self.zone

    def globe_goto(self, zone, types=None, refresh=False):
        self.globe_goto_calls += 1

    def action_point_reusable(self, fresh_ap, cost):
        return self._reusable and fresh_ap is not None

    def action_point_set(self, **kwargs):
        self.action_point_set_calls.append(kwargs)
        return True

    def fleet_set(self, fleet):
        pass

    def os_order_execute(self, **kwargs):
        pass

    def run_strategic_search(self):
        pass

    def _meow_fixed_patrol_scan(self):
        pass

    def handle_after_auto_search(self):
        pass


class TestMeowStayInZoneReuse(unittest.TestCase):
    def test_reuses_reading_within_same_zone(self):
        """已在目标海域 + 读数够 120 → 整段跳过行动点弹窗。"""
        stub = StayStub(current_zone=12)
        OpsiMeowfficerFarming._meow_handle_stay_in_zone(
            stub, SimpleNamespace(zone_id=12), fresh_ap=(1695, 142))
        self.assertEqual(stub.globe_goto_calls, 0)
        self.assertEqual(stub.action_point_set_calls, [])

    def test_insufficient_reading_still_opens_popup(self):
        """读数不够开工线 → 照常走 action_point_set（它会开箱）。"""
        stub = StayStub(current_zone=12, reusable=False)
        OpsiMeowfficerFarming._meow_handle_stay_in_zone(
            stub, SimpleNamespace(zone_id=12), fresh_ap=(1695, 30))
        self.assertEqual(stub.globe_goto_calls, 0)
        self.assertEqual(stub.action_point_set_calls, [
            {'cost': 120, 'keep_current_ap': True, 'check_rest_ap': True}])

    def test_zone_change_discards_reading(self):
        """换海域会消耗行动力 → 读数作废，必须重新走弹窗。"""
        stub = StayStub(current_zone=44)
        OpsiMeowfficerFarming._meow_handle_stay_in_zone(
            stub, SimpleNamespace(zone_id=12), fresh_ap=(1695, 142))
        self.assertEqual(stub.globe_goto_calls, 1)
        self.assertEqual(len(stub.action_point_set_calls), 1)

    def test_not_safe_zone_discards_reading(self):
        """海域名不是安全海域 → 也要重新进海域，读数作废。"""
        stub = StayStub(current_zone=12, name_hidden=False)
        OpsiMeowfficerFarming._meow_handle_stay_in_zone(
            stub, SimpleNamespace(zone_id=12), fresh_ap=(1695, 142))
        self.assertEqual(stub.globe_goto_calls, 1)
        self.assertEqual(len(stub.action_point_set_calls), 1)

    def test_missing_reading_opens_popup(self):
        """独立运行（没有决策首读）→ 走原弹窗流程。"""
        stub = StayStub(current_zone=12)
        OpsiMeowfficerFarming._meow_handle_stay_in_zone(
            stub, SimpleNamespace(zone_id=12), fresh_ap=None)
        self.assertEqual(stub.globe_goto_calls, 0)
        self.assertEqual(len(stub.action_point_set_calls), 1)


if __name__ == '__main__':
    unittest.main()
