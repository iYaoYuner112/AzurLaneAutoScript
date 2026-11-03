# -*- coding: utf-8 -*-
"""每月开荒的跨月保护（对齐 AzurPilot）。

开荒从月底跑到月初时，不能把上个月的“已开荒 100%”写到新月份，
否则新月份会直接被当成完成。开始记当月标记，收尾前核对；跨月就停止。
"""

import os
import sys
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.exception import GameStuckError
from module.os.tasks.explore import OpsiExplore

NEW_RESET = datetime(2026, 12, 1, 0, 0)


class _Context:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class ConfigStub:
    def __init__(self, state):
        self.OpsiExplore_MeowfficerCleanupState = state
        self.OpsiExplore_LastZone = 3
        self.OpsiExplore_SpecialRadar = True
        self.OpsiExplore_ExploreProgress = '已完成百分之50.00'
        self.delays = []
        self.calls = []
        self.stopped = False

    def multi_set(self):
        return _Context()

    def task_delay(self, **kwargs):
        self.delays.append(kwargs)

    def task_call(self, task, **kwargs):
        self.calls.append(task)

    def task_stop(self):
        self.stopped = True


class ExploreStub:
    def __init__(self, state):
        self.config = ConfigStub(state)
        self._os_explore_failed_zone = []


def test_cross_month_end_stops_without_writing_completion():
    stub = ExploreStub({'reset': '2026-11-01T00:00:00', 'phase': 'explore'})
    with patch('module.os.tasks.explore.get_os_next_reset', return_value=NEW_RESET):
        try:
            OpsiExplore._os_explore_end(stub)
        except GameStuckError:
            pass
        else:
            raise AssertionError('跨月收尾应抛 GameStuckError')

    assert stub.config.OpsiExplore_ExploreProgress == '已完成百分之50.00'
    assert stub.config.delays == []
    assert stub.config.calls == []
    assert stub.config.stopped is False


def test_same_month_end_writes_completion_and_releases_daily_shop():
    stub = ExploreStub({'reset': NEW_RESET.isoformat(), 'phase': 'explore'})
    with patch('module.os.tasks.explore.get_os_next_reset', return_value=NEW_RESET):
        OpsiExplore._os_explore_end(stub)

    assert stub.config.OpsiExplore_ExploreProgress == '已完成百分之100.00'
    assert stub.config.OpsiExplore_MeowfficerCleanupState == {
        'reset': NEW_RESET.isoformat(), 'phase': 'done'}
    assert stub.config.delays == [{'target': NEW_RESET}]
    assert stub.config.calls == ['OpsiDaily', 'OpsiShop']
    assert stub.config.stopped is True


def test_new_month_starts_explore_over_after_old_state():
    stub = ExploreStub({'reset': '2026-11-01T00:00:00', 'phase': 'done'})
    stub._os_explore = lambda: None
    with patch('module.os.tasks.explore.get_os_next_reset', return_value=NEW_RESET):
        try:
            OpsiExplore.os_explore(stub)
        except GameStuckError:
            pass

    assert stub.config.OpsiExplore_LastZone == 0
    assert stub.config.OpsiExplore_ExploreProgress == '已完成百分之0.00'
    assert stub.config.OpsiExplore_MeowfficerCleanupState == {
        'reset': NEW_RESET.isoformat(), 'phase': 'explore'}


if __name__ == '__main__':
    test_cross_month_end_stops_without_writing_completion()
    test_same_month_end_writes_completion_and_releases_daily_shop()
    test_new_month_starts_explore_over_after_old_state()
    print('all passed')
