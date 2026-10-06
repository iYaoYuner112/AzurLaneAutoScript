"""补黄币开工线如何传给被代理的那一轮（对齐 AzurPilot 的 ap_preserve 传递）。

调度决策算出本轮阈值后，必须让它真正生效，否则会两头落空：

1. 短猫自己按配置另算一遍保留线（旧代码是 1000）。指定海域循环出击那一路会
   `action_point_set(cost=120, check_rest_ap=True)`，总行动力 600 时直接抛
   `ActionPointLimit` → 被调度层当成「行动力不足」整轮延到明天；
2. 隐秘/深渊/要塞不自己设保留线，代跑期间没有闸 → 能把行动力吃到 0，
   第二天侵蚀1 饿死。

这里锁定四件事：阈值传到了短猫、其余三个任务被 temporary 包住、
打到本轮调度线是优雅回决策而不是延到明天、别的行动力不足仍走原兜底。
"""

import unittest
from datetime import datetime
from types import SimpleNamespace

from module.config.config import TaskEnd
from module.os_handler.action_point import ActionPointLimit
from module.os.tasks.meowfficer_farming import OpsiMeowfficerFarming
from module.os.tasks.scheduling import (
    TASK_NAME_ABYSSAL,
    TASK_NAME_MEOWFFICER_FARMING,
    TASK_NAME_OBSCURE,
    OpsiScheduling,
    OpsiStatus,
)


class PreserveStub:
    """只提供 _meow_preserve_value 需要的配置与月末保留。"""

    def __init__(self, month_end_limit=2000, smart_scheduling=True,
                 scheduling_meow=1000, task_meow=1000):
        self.config = SimpleNamespace(
            OpsiScheduling_MeowfficerActionPointPreserve=scheduling_meow,
            OpsiMeowfficerFarming_ActionPointPreserve=task_meow,
        )
        self.is_smart_scheduling_enabled = smart_scheduling
        self._month_end_limit = month_end_limit

    def get_action_point_limit(self):
        return self._month_end_limit


class TestMeowPreserveValue(unittest.TestCase):
    def test_proxied_round_uses_the_scheduler_line(self):
        """代跑：保留线来自调度层，不再是短猫自己那个 1000。"""
        self.assertEqual(OpsiMeowfficerFarming._meow_preserve_value(PreserveStub(), 200), 200)

    def test_scheduler_line_still_drops_at_month_end(self):
        """月末动态保留仍要叠加，能吃干剩余行动力的那层保护不能丢。"""
        self.assertEqual(
            OpsiMeowfficerFarming._meow_preserve_value(PreserveStub(month_end_limit=0), 200), 0)
        self.assertEqual(
            OpsiMeowfficerFarming._meow_preserve_value(PreserveStub(month_end_limit=300), 1000), 300)

    def test_proxied_line_is_capped_at_2000(self):
        self.assertEqual(
            OpsiMeowfficerFarming._meow_preserve_value(
                PreserveStub(month_end_limit=2000), 100000), 2000)

    def test_standalone_round_keeps_its_own_setting(self):
        """独立跑短猫（智能调度关着）：仍按短猫任务自己的保留值。"""
        stub = PreserveStub(smart_scheduling=False, task_meow=1000)
        self.assertEqual(OpsiMeowfficerFarming._meow_preserve_value(stub), 1000)

    def test_standalone_under_scheduling_uses_the_scheduling_meow_reserve(self):
        stub = PreserveStub(smart_scheduling=True, scheduling_meow=1000)
        self.assertEqual(OpsiMeowfficerFarming._meow_preserve_value(stub), 1000)


class _Backup(object):
    """行为对齐 AzurLaneConfig.temporary：立刻覆盖，退出时还原。"""

    def __init__(self, config, **kwargs):
        self.config = config
        self.old = {}
        for key, value in kwargs.items():
            self.old[key] = getattr(config, key, None)
            setattr(config, key, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for key, value in self.old.items():
            setattr(self.config, key, value)
        return False


def _make_function(command):
    from module.config.config import Function
    return Function({
        'Scheduler': {
            'Command': command,
            'Enable': True,
            'NextRun': datetime(2026, 1, 1),
        }
    })


class ProxyStub:
    """替换掉界面与配置读写，只保留 _run_scheduled_coin_task_once 的派发。"""

    # 这些用例只验证阈值传递与行动力不足的处理，大世界推送关掉（见 test_opsi_notify）
    is_smart_scheduling_enabled = False

    def __init__(self, handler):
        self.config = SimpleNamespace(
            task=_make_function('OpsiScheduling'),
            data={},
            OS_ACTION_POINT_PRESERVE=0,
            temporary=lambda **kwargs: _Backup(self.config, **kwargs),
            bind=lambda task, func_list=None: None,
            task_delay=lambda **kwargs: self.delayed.append(kwargs),
            task_stop=lambda: self._stop(),
        )
        self._handler = handler
        self.delayed = []
        self.closed = 0

    @staticmethod
    def _stop():
        raise TaskEnd

    def _get_coin_task_handler(self, task_name):
        return self._handler

    def _close_scheduling_action_point(self):
        self.closed += 1

    def _postpone_coin_task_check(self, task_name, reason=''):
        pass


class TestCoinTaskThresholdHandoff(unittest.TestCase):
    def test_meowfficer_receives_the_round_threshold(self):
        """短猫拿到 ap_preserve，仍然跳过重复的行动力前置检查。"""
        seen = []

        def meow_once(ap_preserve=None, fresh_ap=None, ap_checked=False):
            seen.append((ap_preserve, fresh_ap, ap_checked))

        stub = ProxyStub(meow_once)
        result = OpsiScheduling._run_scheduled_coin_task_once(
            stub, TASK_NAME_MEOWFFICER_FARMING, 200, fresh_ap=(600, 145))
        self.assertEqual(seen, [(200, (600, 145), True)])
        self.assertEqual(result.status, OpsiStatus.SUCCESS)
        # 短猫跟决策共用同一个面板，不能被提前关掉。
        self.assertEqual(stub.closed, 0)

    def test_other_coin_tasks_run_under_the_scheduler_line(self):
        """隐秘/深渊/要塞代跑期间按本轮阈值设保留线，退出后还原。"""
        seen = []

        def clear_one():
            seen.append(stub.config.OS_ACTION_POINT_PRESERVE)

        stub = ProxyStub(clear_one)
        OpsiScheduling._run_scheduled_coin_task_once(stub, TASK_NAME_OBSCURE, 200)
        self.assertEqual(seen, [200])
        self.assertEqual(stub.config.OS_ACTION_POINT_PRESERVE, 0)
        self.assertEqual(stub.closed, 1)

    def test_reaching_the_scheduling_line_returns_to_the_decision(self):
        """短猫打到本轮调度线 = 正常收尾，交回决策，不延到明天。"""
        def meow_once(**kwargs):
            raise ActionPointLimit(current=45, total=150, preserve=200)

        stub = ProxyStub(meow_once)
        result = OpsiScheduling._run_scheduled_coin_task_once(
            stub, TASK_NAME_MEOWFFICER_FARMING, 200)
        self.assertEqual(result.status, OpsiStatus.SUCCESS)
        self.assertEqual(stub.delayed, [])

    def test_other_action_point_shortage_still_delays_the_scheduler(self):
        """行动力真的不够（开箱也不够开工）：仍按原兜底延到服务器刷新并停。"""
        def clear_one():
            raise ActionPointLimit(current=30, total=150, cost=70)

        stub = ProxyStub(clear_one)
        with self.assertRaises(TaskEnd):
            OpsiScheduling._run_scheduled_coin_task_once(stub, TASK_NAME_ABYSSAL, 200)
        self.assertEqual(stub.delayed, [{'server_update': True}])

    def test_zero_line_never_counts_as_a_graceful_stop(self):
        """月末阈值本就是 0：preserve=0 的 ActionPointLimit 不能当成正常收尾。"""
        def meow_once(**kwargs):
            raise ActionPointLimit(current=0, total=0, preserve=0)

        stub = ProxyStub(meow_once)
        with self.assertRaises(TaskEnd):
            OpsiScheduling._run_scheduled_coin_task_once(stub, TASK_NAME_MEOWFFICER_FARMING, 0)
        self.assertEqual(stub.delayed, [{'server_update': True}])


if __name__ == '__main__':
    unittest.main()
