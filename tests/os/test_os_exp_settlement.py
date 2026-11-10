# -*- coding: utf-8 -*-
"""自律经验结算页：S/A/B/C/D 都要点（对齐 AzurPilot dd30725ed）。

结算页的“确定”和大地图右下角的“作战总览 / 退出”是同一块屏幕位置，
画面已经回到地图时不能再点，否则会把正在跑的自律点停。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.combat.assets import EXP_INFO_A, EXP_INFO_B, EXP_INFO_C, EXP_INFO_D, EXP_INFO_S
from module.os.assets import MAP_EXIT, MAP_GOTO_GLOBE
from module.os_combat.combat import Combat


class ExpStub:
    handle_auto_search_exp_info = Combat.handle_auto_search_exp_info

    def __init__(self, visible, in_map=False):
        self.visible = visible
        self.in_map = in_map
        self.clicks = []
        self.battle_status_click_interval = 0.42
        self.device = self

    def appear(self, button, interval=None, offset=None):
        return button in self.visible

    def is_in_map(self):
        return self.in_map

    def click(self, button):
        self.clicks.append(button)


def test_all_grades_are_clicked_when_map_is_not_on_screen():
    for grade in (EXP_INFO_S, EXP_INFO_A, EXP_INFO_B, EXP_INFO_C, EXP_INFO_D):
        stub = ExpStub({grade})
        assert stub.handle_auto_search_exp_info() is True
        assert stub.clicks == [grade]


def test_s_grade_does_not_fall_through_to_other_grades():
    stub = ExpStub({EXP_INFO_S, EXP_INFO_C})
    assert stub.handle_auto_search_exp_info() is True
    assert stub.clicks == [EXP_INFO_S]


def test_no_grade_visible_returns_false():
    stub = ExpStub(set())
    assert stub.handle_auto_search_exp_info() is False
    assert stub.clicks == []


def test_map_buttons_on_screen_block_the_click():
    for marker in (MAP_GOTO_GLOBE, MAP_EXIT):
        stub = ExpStub({EXP_INFO_S, marker})
        assert stub.handle_auto_search_exp_info() is False
        assert stub.clicks == []


def test_is_in_map_blocks_the_click():
    stub = ExpStub({EXP_INFO_S}, in_map=True)
    assert stub.handle_auto_search_exp_info() is False
    assert stub.clicks == []


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
