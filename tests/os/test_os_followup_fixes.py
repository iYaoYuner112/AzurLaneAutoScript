# -*- coding: utf-8 -*-
"""AP 10-17/11-03 两个大世界跟进修复。

1. 新一轮大世界自律开始前清掉上一轮的重试预算（AP d90245a18）。
2. 任务列表两个循环点掉延迟弹出的「合计获得奖励」结算（AP d07a0d45f）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.base.timer import Timer
from module.os_handler.assets import AUTO_SEARCH_REWARD
from module.os_handler.map_event import MapEventHandler
from module.os_handler.mission import MissionHandler

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MissionStub:
    def __init__(self):
        self.clicks = []
        self._os_mission_submitted = False
        self._os_mission_index = 0

    def loop(self):
        return iter(range(1))

    def is_in_os_mission(self):
        return False

    def is_in_opsi_explore(self):
        return False

    def is_zone_pinned(self):
        return False

    def is_in_map(self):
        return False

    def appear(self, *args, **kwargs):
        return False

    def match_template_color(self, *args, **kwargs):
        return False

    def appear_then_click(self, button, **kwargs):
        self.clicks.append(button.name)
        return button is AUTO_SEARCH_REWARD

    def handle_popup_confirm(self, *args, **kwargs):
        return False

    def handle_map_get_items(self):
        return False

    def handle_info_bar(self):
        return False


class MissionRewardPopupTest(unittest.TestCase):
    def test_mission_enter_clicks_delayed_reward(self):
        stub = MissionStub()
        MissionHandler.os_mission_enter(stub)
        self.assertIn(AUTO_SEARCH_REWARD.name, stub.clicks)

    def test_checkout_loop_clicks_delayed_reward(self):
        stub = MissionStub()
        stub.os_mission_enter = lambda **kwargs: (-20, -20, 20, 20)
        stub._os_find_checkout_offset_skip_monthly_boss = lambda offset, **kwargs: offset
        MissionHandler.os_get_next_mission(stub)
        self.assertIn(AUTO_SEARCH_REWARD.name, stub.clicks)


class AutoSearchBudgetTest(unittest.TestCase):
    def test_budget_clear_stops_retry_timer(self):
        stub = MapEventHandler.__new__(MapEventHandler)
        stub._os_auto_search_enable_timer = Timer(45).start()
        stub._os_auto_search_enable_budget_clear()
        self.assertFalse(stub._os_auto_search_enable_timer.started())

    def test_new_round_clears_budget(self):
        path = os.path.join(ROOT, 'module', 'os', 'map.py')
        with open(path, encoding='utf-8') as f:
            source = f.read()
        round_start = source.index('def os_auto_search_daemon(')
        self.assertIn('self._os_auto_search_enable_budget_clear()', source[round_start:])


if __name__ == '__main__':
    unittest.main()
