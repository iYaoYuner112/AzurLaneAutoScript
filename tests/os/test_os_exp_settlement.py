# -*- coding: utf-8 -*-
"""自律经验结算页：S/A/B/C/D 都要点（对齐 AzurPilot dd30725ed）。

旧实现只处理 C/D，自律被游戏或任务打断后停在 S/A/B 结算页会一直等到卡死。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.combat.assets import EXP_INFO_A, EXP_INFO_B, EXP_INFO_C, EXP_INFO_D, EXP_INFO_S
from module.os_combat.combat import Combat


class ExpStub:
    handle_auto_search_exp_info = Combat.handle_auto_search_exp_info

    def __init__(self, visible):
        self.visible = visible
        self.clicks = []
        self.battle_status_click_interval = 0.42

    def appear_then_click(self, button, interval=None):
        if button in self.visible:
            self.clicks.append((button, interval))
            return True
        return False


def test_all_grades_are_clicked_with_battle_interval():
    for grade in (EXP_INFO_S, EXP_INFO_A, EXP_INFO_B, EXP_INFO_C, EXP_INFO_D):
        stub = ExpStub({grade})
        assert stub.handle_auto_search_exp_info() is True
        assert stub.clicks == [(grade, 0.42)]


def test_s_grade_does_not_fall_through_to_other_grades():
    stub = ExpStub({EXP_INFO_S, EXP_INFO_C})
    assert stub.handle_auto_search_exp_info() is True
    assert [button for button, _ in stub.clicks] == [EXP_INFO_S]


def test_no_grade_visible_returns_false():
    stub = ExpStub(set())
    assert stub.handle_auto_search_exp_info() is False
    assert stub.clicks == []


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
