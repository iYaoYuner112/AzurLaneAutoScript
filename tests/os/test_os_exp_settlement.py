# -*- coding: utf-8 -*-
"""自律经验结算页：S/A/B/C/D 都要点（对齐 AzurPilot dd30725ed）。

结算页的“确定”和大地图右下角的“作战总览 / 退出”是同一块屏幕位置。
结算页翻页约 1 秒，脚本每秒匹配一次评级，连点会让第二下落到已经切回的
地图上；点过一次后 3 秒内不重复点，等页面自己翻走，超时还停着才补点。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.combat.assets import EXP_INFO_A, EXP_INFO_B, EXP_INFO_C, EXP_INFO_D, EXP_INFO_S
from module.os_combat.combat import Combat


class FakeTimer:
    def __init__(self, ready=True):
        self.ready = ready

    def reached(self):
        return self.ready

    def reset(self):
        self.ready = False


class ExpStub:
    handle_auto_search_exp_info = Combat.handle_auto_search_exp_info

    def __init__(self, visible, timer=None):
        self.visible = visible
        self.clicks = []
        self.battle_status_click_interval = 0.42
        self._exp_info_click_timer = timer or FakeTimer()
        self.device = self

    def appear(self, button, interval=None):
        return button in self.visible

    def click(self, button):
        self.clicks.append(button)


def test_all_grades_are_clicked():
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


def test_repeated_click_within_cooldown_is_suppressed():
    timer = FakeTimer()
    stub = ExpStub({EXP_INFO_S}, timer=timer)
    assert stub.handle_auto_search_exp_info() is True
    assert stub.clicks == [EXP_INFO_S]
    # 2 秒内同一张结算页还在：不再点
    assert stub.handle_auto_search_exp_info() is False
    assert stub.clicks == [EXP_INFO_S]
    # 超过 2 秒还停着：补点一次
    timer.ready = True
    assert stub.handle_auto_search_exp_info() is True
    assert stub.clicks == [EXP_INFO_S, EXP_INFO_S]


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
