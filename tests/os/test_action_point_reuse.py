"""行动点复用（对齐 AzurPilot 的 action_point_reusable / _prepare_scheduling_action_point）。

智能调度决策读行动力时会**暂留面板**，子任务开工时优先复用那次读数，省掉
「关窗 → 重开 → 重读」的一趟往返。这里锁定两件事：

- `action_point_reusable()` 的判定边界：只有「弹窗口径总行动力高于保留线」
  且「当前行动力达到开工线」时才允许跳过弹窗，否则必须照常走 `action_point_set`
  （开行动力箱和石油购买都在那里）；
- `_prepare_scheduling_action_point()` 的面板生命周期：没暂留面板时原样返回，
  暂留了就先收尾（够用就关窗复用，不够就在同一面板里补充）。
"""

import unittest
from types import SimpleNamespace

from module.os.tasks.scheduling import OpsiScheduling
from module.os_handler.action_point import ActionPointHandler


class ReusableStub:
    """只提供 action_point_reusable 需要的配置。"""

    def __init__(self, preserve=200):
        self.config = SimpleNamespace(OS_ACTION_POINT_PRESERVE=preserve)

    action_point_reusable = ActionPointHandler.action_point_reusable


class TestActionPointReusable(unittest.TestCase):
    def test_missing_reading_is_not_reusable(self):
        stub = ReusableStub()
        self.assertFalse(ActionPointHandler.action_point_reusable(stub, None, 70))

    def test_total_at_or_below_preserve_is_not_reusable(self):
        """总行动力没超过保留线 → 弹窗路径会拦下来，不能跳过。"""
        stub = ReusableStub(preserve=200)
        self.assertFalse(ActionPointHandler.action_point_reusable(stub, (200, 200), 70))
        self.assertFalse(ActionPointHandler.action_point_reusable(stub, (150, 150), 70))

    def test_current_below_cost_is_not_reusable(self):
        """总行动力够，但当前行动力不够开工线 → 还得开箱/购买。"""
        stub = ReusableStub(preserve=200)
        self.assertFalse(ActionPointHandler.action_point_reusable(stub, (1417, 69), 70))

    def test_reusable_when_both_thresholds_met(self):
        stub = ReusableStub(preserve=200)
        self.assertTrue(ActionPointHandler.action_point_reusable(stub, (1417, 70), 70))
        self.assertTrue(ActionPointHandler.action_point_reusable(stub, (1417, 500), 70))


class PrepareStub:
    """替换界面交互，只保留 _prepare_scheduling_action_point 的决策分支。"""

    def __init__(self, panel_open, in_action_point=True, reusable=False,
                 box_use_note=7, handle_ok=True):
        self._scheduling_ap_panel_open = panel_open
        self._scheduling_ap_box_use = box_use_note
        self.config = SimpleNamespace(OS_ACTION_POINT_BOX_USE=box_use_note)
        self._in_action_point_flag = in_action_point
        self._reusable = reusable
        self._handle_ok = handle_ok
        self._action_point_total = 1417
        self._action_point_current = 420
        self.quit_calls = 0
        self.handle_calls = []

    def _is_in_action_point(self):
        return self._in_action_point_flag

    def action_point_reusable(self, fresh_ap, cost):
        return self._reusable

    def action_point_quit(self):
        self.quit_calls += 1

    def handle_action_point(self, **kwargs):
        self.handle_calls.append(kwargs)
        return self._handle_ok


class TestPrepareSchedulingActionPoint(unittest.TestCase):
    def test_without_open_panel_returns_reading_unchanged(self):
        """面板没被暂留（独立运行 / 被打断）→ 原样返回，子任务照常走弹窗。"""
        stub = PrepareStub(panel_open=False)
        result = OpsiScheduling._prepare_scheduling_action_point(stub, (1417, 420), cost=70)
        self.assertEqual(result, (1417, 420))
        self.assertEqual(stub.quit_calls, 0)
        self.assertEqual(stub.handle_calls, [])

    def test_panel_gone_returns_none(self):
        """面板被别的流程关掉了 → 返回 None，交给子任务重新走弹窗。"""
        stub = PrepareStub(panel_open=True, in_action_point=False)
        result = OpsiScheduling._prepare_scheduling_action_point(stub, (1417, 420), cost=70)
        self.assertIsNone(result)
        self.assertEqual(stub.handle_calls, [])

    def test_reusable_reading_closes_panel_and_reuses(self):
        """读数够开工 → 关窗 + 原样返回读数，子任务跳过弹窗。"""
        stub = PrepareStub(panel_open=True, reusable=True)
        result = OpsiScheduling._prepare_scheduling_action_point(stub, (1417, 420), cost=70)
        self.assertEqual(result, (1417, 420))
        self.assertEqual(stub.quit_calls, 1)
        self.assertEqual(stub.handle_calls, [])
        self.assertFalse(stub._scheduling_ap_panel_open)

    def test_insufficient_reading_tops_up_in_same_panel(self):
        """不够开工 → 在同一个面板里补充，返回补充后的新读数。"""
        stub = PrepareStub(panel_open=True, reusable=False)
        result = OpsiScheduling._prepare_scheduling_action_point(stub, (1417, 30), cost=70)
        self.assertEqual(result, (1417, 420))
        # 补充完成后由调度层统一关窗，返回新读数给子任务
        self.assertEqual(stub.quit_calls, 1)
        self.assertEqual(len(stub.handle_calls), 1)
        # 含箱口径一致时可以跳过重复首读
        self.assertTrue(stub.handle_calls[0]['skip_first_read'])

    def test_boxes_changed_makes_it_read_again(self):
        """含箱口径变了（玩家中途改了开箱开关）→ 不能复用首读。"""
        stub = PrepareStub(panel_open=True, reusable=True, box_use_note=7)
        stub.config.OS_ACTION_POINT_BOX_USE = 8
        result = OpsiScheduling._prepare_scheduling_action_point(stub, (1417, 420), cost=70)
        # 口径不一致 → 走补充路径，并且不跳过首读
        self.assertEqual(stub.quit_calls, 1)
        self.assertEqual(len(stub.handle_calls), 1)
        self.assertFalse(stub.handle_calls[0]['skip_first_read'])
        self.assertEqual(result, (1417, 420))

    def test_top_up_failure_closes_panel_and_returns_none(self):
        """补充失败（例如行动力不足）→ 关窗并返回 None。"""
        stub = PrepareStub(panel_open=True, handle_ok=False)
        result = OpsiScheduling._prepare_scheduling_action_point(stub, (1417, 30), cost=70)
        self.assertIsNone(result)
        self.assertEqual(stub.quit_calls, 1)


if __name__ == '__main__':
    unittest.main()
