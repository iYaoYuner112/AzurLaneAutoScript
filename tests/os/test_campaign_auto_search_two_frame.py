# -*- coding: utf-8 -*-
"""自律开关两帧确认（对齐 AzurPilot aa29d393d）。

战斗结算退场后约 1.8 秒内点开关，图标会翻面但游戏不下发移动指令，舰队静止。
改为两帧确认：第一张截图只记录，第二张不同截图仍为 OFF 才点击；ON、空白、
加载/战斗画面都会清掉旧确认，避免把跨转场的 OFF 拼成两帧。
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.base.timer import Timer
from module.combat.auto_search_combat import AutoSearchCombat
from module.handler.assets import AUTO_SEARCH_MAP_OPTION_OFF
from module.handler.auto_search import AutoSearchHandler


class SwitchStub:
    _auto_search_offset = (5, 5)
    _auto_search_off_frame = None
    handle_auto_search_map_option = AutoSearchHandler.handle_auto_search_map_option

    def __init__(self, off=True):
        self.off = off
        self.clicks = []
        self.device = SimpleNamespace(image=object())

    def match_template_color(self, button, **kwargs):
        return self.off and button is AUTO_SEARCH_MAP_OPTION_OFF

    def appear_then_click(self, button, **kwargs):
        self.clicks.append(button.name)
        return True

    def next_frame(self, off=True):
        self.off = off
        self.device.image = object()


class TwoFrameTest(unittest.TestCase):
    def test_two_different_off_frames_are_required(self):
        stub = SwitchStub(off=True)
        self.assertFalse(stub.handle_auto_search_map_option())
        self.assertEqual(stub.clicks, [])

        # 同一张截图重复调用，不算第二帧
        self.assertFalse(stub.handle_auto_search_map_option())
        self.assertEqual(stub.clicks, [])

        stub.next_frame(off=True)
        self.assertTrue(stub.handle_auto_search_map_option())
        self.assertEqual(stub.clicks, [AUTO_SEARCH_MAP_OPTION_OFF.name])
        self.assertIsNone(stub._auto_search_off_frame)

    def test_on_frame_clears_the_first_confirmation(self):
        stub = SwitchStub(off=True)
        stub.handle_auto_search_map_option()  # 第一帧 OFF

        stub.next_frame(off=False)  # 开关已翻面
        self.assertFalse(stub.handle_auto_search_map_option())
        self.assertIsNone(stub._auto_search_off_frame)

        # 再次 OFF 时重新从第一帧开始，不能直接点
        stub.next_frame(off=True)
        self.assertFalse(stub.handle_auto_search_map_option())
        self.assertEqual(stub.clicks, [])

    def test_blank_frame_clears_the_first_confirmation(self):
        stub = SwitchStub(off=True)
        stub.handle_auto_search_map_option()

        stub.next_frame(off=False)  # 空白/转场，没有 OFF 也没有 ON
        self.assertFalse(stub.handle_auto_search_map_option())
        self.assertIsNone(stub._auto_search_off_frame)

        # 空白之后的 OFF 必须重新凑两帧
        stub.next_frame(off=True)
        self.assertFalse(stub.handle_auto_search_map_option())
        stub.next_frame(off=True)
        self.assertTrue(stub.handle_auto_search_map_option())
        self.assertEqual(stub.clicks, [AUTO_SEARCH_MAP_OPTION_OFF.name])


class ResumeResetTest(unittest.TestCase):
    def test_prepare_recovery_clears_stale_confirmation(self):
        stub = AutoSearchCombat.__new__(AutoSearchCombat)
        stub._auto_search_off_frame = object()
        stub._auto_search_status_confirm = True
        stub._auto_search_in_stage_timer = Timer(3, count=6)
        stub.is_auto_search_running = lambda: False

        self.assertTrue(stub.prepare_auto_search_recovery('map'))
        self.assertIsNone(stub._auto_search_off_frame)
        self.assertTrue(stub._restart_resume_guard)


if __name__ == '__main__':
    unittest.main()
