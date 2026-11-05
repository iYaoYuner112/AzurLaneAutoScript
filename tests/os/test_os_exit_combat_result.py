# -*- coding: utf-8 -*-
"""离港时处理战斗结算页的回归测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.os.globe_operation import GlobeOperation

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CombatResultExitTest(unittest.TestCase):
    def test_without_combat_methods(self):
        self.assertFalse(GlobeOperation.handle_combat_result_on_exit(object()))

    def test_handles_battle_status_first(self):
        calls = []

        class Stub:
            def handle_battle_status(self):
                calls.append('battle')
                return True

            def handle_exp_info(self):
                calls.append('exp')
                return True

        self.assertTrue(GlobeOperation.handle_combat_result_on_exit(Stub()))
        self.assertEqual(calls, ['battle'])

    def test_handles_exp_info_when_battle_status_misses(self):
        calls = []

        class Stub:
            def handle_battle_status(self):
                calls.append('battle')
                return False

            def handle_exp_info(self):
                calls.append('exp')
                return True

        self.assertTrue(GlobeOperation.handle_combat_result_on_exit(Stub()))
        self.assertEqual(calls, ['battle', 'exp'])

    def test_exit_loops_call_the_helper(self):
        path = os.path.join(ROOT, 'module', 'os', 'globe_operation.py')
        with open(path, encoding='utf-8') as f:
            source = f.read()
        start = source.index('def os_map_goto_globe(')
        self.assertIn('if self.handle_combat_result_on_exit():', source[start:])

        path = os.path.join(ROOT, 'module', 'os', 'globe_camera.py')
        with open(path, encoding='utf-8') as f:
            source = f.read()
        start = source.index('def globe_update(')
        self.assertIn('if self.handle_combat_result_on_exit():', source[start:])


if __name__ == '__main__':
    unittest.main()
