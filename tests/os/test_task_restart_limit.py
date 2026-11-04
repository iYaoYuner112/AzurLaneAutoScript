# -*- coding: utf-8 -*-
"""连续恢复上限与故障冷却的离线测试（对齐 AzurPilot f19f708bd）。"""
import ast
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from alas import AzurLaneAutoScript


def make_script(limit=3):
    """构造一个不触碰设备/配置文件的调度器桩。"""
    script = object.__new__(AzurLaneAutoScript)
    script.config_name = 'test'
    script.failure_record = {}
    script.task_restart_record = {}
    script.task_restart_delays = {}
    script.config = SimpleNamespace(
        Error_TaskRestartLimit=limit,
        Error_OnePushConfig='provider: null',
        task_delay=Mock(),
        cross_get=Mock(return_value=None),
        cross_set=Mock(),
    )
    return script


class RecordRestartTest(unittest.TestCase):
    def setUp(self):
        self.future = datetime.now() + timedelta(hours=2)
        self.patcher = patch('alas.get_server_next_update', return_value=self.future)
        self.notify = patch('alas.handle_notify')
        self.patcher.start()
        self.notify.start()

    def tearDown(self):
        self.patcher.stop()
        self.notify.stop()

    def test_restart_task_never_cools_down(self):
        script = make_script()
        for _ in range(5):
            self.assertFalse(script._record_task_restart('Restart', False))
        self.assertEqual(script.task_restart_record, {})
        self.assertEqual(script.task_restart_delays, {})
        script.config.task_delay.assert_not_called()

    def test_success_clears_counter(self):
        script = make_script()
        script._record_task_restart('Main', False)
        self.assertEqual(script.task_restart_record['Main'], 1)
        script._record_task_restart('Main', True)
        self.assertNotIn('Main', script.task_restart_record)

    def test_below_limit_retries_immediately(self):
        script = make_script(limit=3)
        self.assertFalse(script._record_task_restart('GemsFarming', False))
        self.assertFalse(script._record_task_restart('GemsFarming', False))
        script.config.task_delay.assert_not_called()
        self.assertEqual(script.task_restart_record['GemsFarming'], 2)

    def test_reaching_limit_defers_task_instead_of_exit(self):
        script = make_script(limit=3)
        script.failure_record['GemsFarming'] = 2
        self.assertFalse(script._record_task_restart('GemsFarming', False))
        self.assertFalse(script._record_task_restart('GemsFarming', False))
        self.assertTrue(script._record_task_restart('GemsFarming', False))
        script.config.task_delay.assert_called_once_with(target=self.future, task='GemsFarming')
        self.assertEqual(script.task_restart_delays['GemsFarming'], self.future)
        # 冷却时清空两类计数，冷却结束后重新从 1 开始
        self.assertNotIn('GemsFarming', script.task_restart_record)
        self.assertNotIn('GemsFarming', script.failure_record)

    def test_limit_disabled_clears_records(self):
        script = make_script(limit=0)
        script.task_restart_record['Main'] = 2
        script.task_restart_delays['Main'] = self.future
        self.assertFalse(script._record_task_restart('Main', False))
        self.assertNotIn('Main', script.task_restart_record)
        self.assertNotIn('Main', script.task_restart_delays)
        script.config.task_delay.assert_not_called()

    def test_tasks_are_independent(self):
        script = make_script(limit=2)
        script._record_task_restart('Main', False)
        script._record_task_restart('Event', False)
        script._record_task_restart('Event', False)
        self.assertEqual(script.task_restart_record.get('Main'), 1)
        self.assertIn('Event', script.task_restart_delays)


class ReleaseCooldownTest(unittest.TestCase):
    def test_release_only_when_schedule_untouched(self):
        script = make_script()
        deadline = datetime.now() + timedelta(hours=5)
        script.task_restart_delays['Main'] = deadline
        script.config.cross_get = Mock(return_value=deadline)
        self.assertTrue(script._release_restart_cooldown('Main'))
        script.config.cross_set.assert_called_once()
        self.assertNotIn('Main', script.task_restart_delays)

    def test_release_skipped_when_user_rescheduled(self):
        script = make_script()
        deadline = datetime.now() + timedelta(hours=5)
        script.task_restart_delays['Main'] = deadline
        script.config.cross_get = Mock(return_value=datetime.now() + timedelta(hours=1))
        self.assertFalse(script._release_restart_cooldown('Main'))
        script.config.cross_set.assert_not_called()

    def test_release_all(self):
        script = make_script()
        deadline = datetime.now() + timedelta(hours=5)
        script.task_restart_delays['A'] = deadline
        script.task_restart_delays['B'] = deadline
        script.config.cross_get = Mock(return_value=deadline)
        script._release_restart_cooldowns()
        self.assertEqual(script.task_restart_delays, {})


class StaticIntegrationTest(unittest.TestCase):
    """防误删：alas.py 的返回值与循环钩子必须存在。"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cls.src = open(os.path.join(root, 'alas.py'), encoding='utf-8').read()

    def test_recoverable_returns(self):
        tree = ast.parse(self.src)
        # 收集 run() 里各 except 分支的 return 值
        returns = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant):
                        names = []
                        for t in ast.walk(node.type) if node.type is not None else []:
                            if isinstance(t, ast.Name):
                                names.append(t.id)
                        if names:
                            returns[tuple(names)] = sub.value.value
        for exc in [('GameNotRunningError',), ('GameStuckError', 'GameTooManyClickError'),
                    ('GameBugError',), ('RequestHumanTakeover',), ('EmulatorNotRunningError',)]:
            self.assertEqual(returns.get(exc), 'recoverable', f'{exc} 应返回 recoverable')

    def test_loop_hooks(self):
        for token in ['deferred = self._record_task_restart(task, success)',
                      'self.task_restart_delays.get(task)',
                      'self._release_restart_cooldowns()']:
            self.assertIn(token, self.src, f'alas.py 缺少钩子: {token}')

    def test_config_key(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        gen = open(os.path.join(root, 'module', 'config', 'config_generated.py'), encoding='utf-8').read()
        self.assertIn('Error_TaskRestartLimit', gen)


class EnvironmentRestartTest(unittest.TestCase):
    def make_runner(self, error):
        script = object.__new__(AzurLaneAutoScript)
        script.config = SimpleNamespace(task_call=Mock())
        script._notify_recoverable = Mock()

        def boom():
            raise error

        script.boom = boom
        return script

    def test_environment_errors_do_not_consume_restart_budget(self):
        script = make_script()
        self.assertFalse(script._should_count_task_restart('recoverable', True))
        self.assertTrue(script._should_count_task_restart('recoverable', False))
        self.assertTrue(script._should_count_task_restart(True, True))

    def test_game_not_running_marks_environment_error(self):
        from module.exception import GameNotRunningError

        script = self.make_runner(GameNotRunningError('game died'))
        assert script.run('boom', skip_first_screenshot=True) == 'recoverable'
        assert script._recoverable_environment_error is True
        script.config.task_call.assert_called_once_with('Restart')

    def test_emulator_offline_marks_environment_error(self):
        from module.exception import EmulatorNotRunningError

        script = self.make_runner(EmulatorNotRunningError('adb offline'))
        assert script.run('boom', skip_first_screenshot=True) == 'recoverable'
        assert script._recoverable_environment_error is True
        script.config.task_call.assert_called_once_with('Restart')


if __name__ == '__main__':
    unittest.main()
