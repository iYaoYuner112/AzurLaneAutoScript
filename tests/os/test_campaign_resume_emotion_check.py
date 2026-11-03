# -*- coding: utf-8 -*-
"""续战后的接替舰队心情预检（对齐 AzurPilot check_fleet）。

正常进图前有 check_reduce 按整图预计消耗检查；续战跳过进图，
由 check_fleet 在开自律前补一次单场检查：不够就延后任务并撤退。
"""

import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.base.timer import Timer
from module.combat.auto_search_combat import AutoSearchCombat
from module.combat.emotion import Emotion
from module.exception import ScriptEnd


class ConfigStub:
    def __init__(self, mode='calculate', value=2):
        now = datetime.now()
        self.Emotion_Mode = mode
        self.Fleet_FleetOrder = 'fleet1_mob_fleet2_boss'
        for fleet in (1, 2):
            setattr(self, f'Emotion_Fleet{fleet}Value', value if fleet == 1 else 150)
            setattr(self, f'Emotion_Fleet{fleet}Record', now)
            setattr(self, f'Emotion_Fleet{fleet}Recover', 'not_in_dormitory')
            setattr(self, f'Emotion_Fleet{fleet}Control', 'prevent_red_face')
            setattr(self, f'Emotion_Fleet{fleet}Oath', False)
        self.delay = None

    def task_delay(self, **kwargs):
        self.delay = kwargs

    def set_record(self, **kwargs):
        pass


class CheckFleetTest(unittest.TestCase):
    def test_low_emotion_delays_and_stops(self):
        config = ConfigStub(value=2)
        emotion = Emotion(config)

        with self.assertRaises(ScriptEnd):
            emotion.check_fleet(1, after_restart=True)

        self.assertIsNotNone(config.delay, '心情不足时应延后任务')
        self.assertIn('target', config.delay)

    def test_enough_emotion_continues(self):
        config = ConfigStub(value=50)
        emotion = Emotion(config)

        emotion.check_fleet(1, after_restart=True)
        self.assertIsNone(config.delay, '心情足够时不应延后任务')

    def test_ignore_mode_skips_check(self):
        config = ConfigStub(mode='ignore', value=0)
        emotion = Emotion(config)

        emotion.check_fleet(1, after_restart=True)
        self.assertIsNone(config.delay, '忽略心情模式不应延后任务')


class TimedReached:
    def reached(self):
        return True


class ResumeGuardTest(unittest.TestCase):
    def make_stub(self):
        stub = AutoSearchCombat.__new__(AutoSearchCombat)
        stub._restart_resume_guard = True
        stub._restart_resume_timer = Timer(30, count=60).start()
        stub._restart_resume_click_timer = TimedReached()
        stub._restart_emotion_checked = False
        stub._auto_search_fleet_confirmed = False
        stub._auto_search_off_frame = None
        stub.fleet_show_index = 1
        stub.is_auto_search_running = lambda: False
        stub.is_combat_loading = lambda: False
        stub.is_combat_executing = lambda: False
        stub.match_template_color = lambda *args, **kwargs: True
        stub.withdraw = Mock()
        return stub

    def test_low_emotion_withdraws_before_enabling_auto_search(self):
        stub = self.make_stub()
        stub.auto_search_watch_fleet = lambda: setattr(
            stub, '_auto_search_fleet_confirmed', True)
        stub.emotion = SimpleNamespace(check_fleet=Mock(side_effect=ScriptEnd('low')))

        with self.assertRaises(ScriptEnd):
            stub.handle_auto_search_map_option()

        stub.emotion.check_fleet.assert_called_once_with(1, after_restart=True)
        stub.withdraw.assert_called_once_with()

    def test_unconfirmed_fleet_waits_instead_of_checking(self):
        stub = self.make_stub()
        stub.auto_search_watch_fleet = lambda: None
        stub.emotion = SimpleNamespace(check_fleet=Mock())

        self.assertFalse(stub.handle_auto_search_map_option())
        stub.emotion.check_fleet.assert_not_called()
        stub.withdraw.assert_not_called()


if __name__ == '__main__':
    unittest.main()
