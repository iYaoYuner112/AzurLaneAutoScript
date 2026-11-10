"""战斗结算窗口内不能点「自律寻敌」开关（对齐 AzurPilot）。

实机案例（2026-10-31 月末清理）：战斗结束后 0.5 秒按钮还是关闭外观，我们点了一下，
既把仍在执行的自律寻敌点停，又启动了 45 秒重试预算；本海域靠奖励收尾后预算没清，
45 秒后进下一个海域第一次点击就误判「连续点击 45 秒未生效」并重启游戏。

AP 的做法是战斗结束记一个 2 秒窗口，窗口内不操作地图自律开关。
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.base.timer import Timer
from module.handler.assets import STORY_SKIP_3
from module.os_handler.assets import (
    AUTO_SEARCH_OS_MAP_OPTION_OFF,
    AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED,
    AUTO_SEARCH_OS_MAP_OPTION_ON,
    AUTO_SEARCH_REWARD,
)
from module.os_handler.map_event import MapEventHandler


class VirtualClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Stub(MapEventHandler):
    def __init__(self):
        self.config = SimpleNamespace(task=SimpleNamespace(command='OpsiDaily'))
        self.clicks = []
        self.interval_timer = {}
        self.device = SimpleNamespace(
            click=lambda button, *a, **k: self.clicks.append(button.name),
            click_record_clear=lambda: None,
            screenshot_interval_set=lambda *a, **k: None,
        )

    def match_template_color(self, button, **kwargs):
        return button.name == AUTO_SEARCH_OS_MAP_OPTION_OFF.name

    def appear(self, button, **kwargs):
        return False

    def info_bar_count(self):
        return 0

    def is_in_map(self):
        return True


def set_map_state(stub, *, off=False, off_disabled=False, on=False,
                  reward=False, story=False, in_map=True):
    visible = set()
    if off:
        visible.add(AUTO_SEARCH_OS_MAP_OPTION_OFF.name)
    if off_disabled:
        visible.add(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED.name)
    if on:
        visible.add(AUTO_SEARCH_OS_MAP_OPTION_ON.name)
    stub.match_template_color = lambda button, **kwargs: button.name in visible

    appeared = set()
    if reward:
        appeared.add(AUTO_SEARCH_REWARD.name)
    if story:
        appeared.add(STORY_SKIP_3.name)
    stub.appear = lambda button, **kwargs: button.name in appeared
    stub.is_in_map = lambda: in_map


class SettleTest(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        self.time_patch = patch('module.base.timer.time', self.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def test_fresh_handler_is_not_settling(self):
        stub = Stub()
        self.assertFalse(stub._os_auto_search_battle_settling())

    def test_settle_window_blocks_enable_click(self):
        """战斗刚结束的 2 秒内：不点按钮，也不启动 45 秒预算。"""
        stub = Stub()
        stub._os_auto_search_battle_settle_start()
        self.assertTrue(stub._os_auto_search_battle_settling())

        self.assertFalse(stub.handle_os_auto_search_map_option())
        self.assertEqual(stub.clicks, [])
        self.assertNotIn('_os_auto_search_enable_timer', stub.__dict__,
                         '结算窗口内不该启动自律重试预算')

    def test_click_resumes_after_settle_window(self):
        """2 秒窗口过后恢复正常点击。"""
        stub = Stub()
        stub._os_auto_search_battle_settle_start()
        self.clock.advance(2.1)

        self.assertFalse(stub._os_auto_search_battle_settling())
        self.assertTrue(stub.handle_os_auto_search_map_option())
        self.assertEqual(stub.clicks, [AUTO_SEARCH_OS_MAP_OPTION_OFF.name])

    def test_reward_clears_enable_budget(self):
        """领奖表示本轮自律结束，45 秒开启预算要清零。"""
        stub = Stub()
        stub._os_auto_search_enable_timer = Timer(45).start()
        set_map_state(stub, reward=True)
        stub.os_auto_search_quit = lambda drop=None: False

        self.assertTrue(stub.handle_os_auto_search_map_option())
        self.assertFalse(stub._os_auto_search_enable_timer.started())

    def test_unconfirmed_disappearance_keeps_budget(self):
        """按钮消失但没看到 ON/剧情/离开地图，不能当成已开启而清零。"""
        stub = Stub()
        stub._os_auto_search_enable_timer = Timer(45).start()
        set_map_state(stub)

        self.assertFalse(stub.handle_os_auto_search_map_option())
        self.assertTrue(stub._os_auto_search_enable_timer.started())

    def test_confirmed_on_clears_budget(self):
        """确认自律已开启时清零。"""
        stub = Stub()
        stub._os_auto_search_enable_timer = Timer(45).start()
        set_map_state(stub, on=True)

        self.assertFalse(stub.handle_os_auto_search_map_option())
        self.assertFalse(stub._os_auto_search_enable_timer.started())

    def test_story_clears_budget(self):
        """剧情挡住界面时清零。"""
        stub = Stub()
        stub._os_auto_search_enable_timer = Timer(45).start()
        set_map_state(stub, story=True)

        self.assertFalse(stub.handle_os_auto_search_map_option())
        self.assertFalse(stub._os_auto_search_enable_timer.started())


if __name__ == '__main__':
    unittest.main()
