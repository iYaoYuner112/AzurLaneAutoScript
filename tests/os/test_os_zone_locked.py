# -*- coding: utf-8 -*-
"""锁定海域（周边海域未压制）的识别与次日延期（对齐 AzurPilot 95a9961ee）。"""

import os
import sys
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.campaign.os_run import OSCampaignRun
from module.config.utils import get_server_next_update
from module.os.globe_operation import OSExploreError, OSZoneLockedError
from module.os.tasks.prevent_action_point_overflow import (
    TASK_NAME as PREVENT_TASK_NAME,
    OpsiPreventActionPointOverflow,
)
from module.os.tasks.task_context import delay_task_for_locked_zone, task_zone_locked_until


class FakeConfig:
    def __init__(self, task='OpsiShop'):
        self.task = SimpleNamespace(command=task)
        self.storage = {}
        self.delayed = []
        self.stopped = False

    def cross_get(self, key, default=None):
        return self.storage.get(key, default)

    def cross_set(self, key, value):
        self.storage[key] = value

    @contextmanager
    def multi_set(self):
        yield

    def task_delay(self, **kwargs):
        self.delayed.append(kwargs)

    def task_stop(self):
        self.stopped = True


class PreventStub:
    update_prevent_action_point_overflow_schedule = (
        OpsiPreventActionPointOverflow.update_prevent_action_point_overflow_schedule
    )

    def __init__(self, locked_until=None):
        self._locked_until = locked_until
        self.config = FakeConfig()

    def _get_current_action_point_for_overflow(self):
        return 50

    def _get_thresholds(self):
        return 200, 10


def test_zone_locked_is_a_specific_explore_error():
    assert issubclass(OSZoneLockedError, OSExploreError)


def test_locked_zone_delays_current_task_to_next_day():
    config = FakeConfig('OpsiShop')
    delay_task_for_locked_zone(config)
    until = get_server_next_update('00:00')
    assert config.storage['OpsiShop.Storage.Storage.ZoneLockedUntil'] == until.isoformat()
    assert config.delayed == [{'target': until}]
    assert config.stopped is True
    assert task_zone_locked_until(config, 'OpsiShop') == until
    assert task_zone_locked_until(config, 'OpsiDaily') is None


def test_stale_locked_record_is_ignored():
    config = FakeConfig('OpsiShop')
    config.storage['OpsiShop.Storage.Storage.ZoneLockedUntil'] = '2000-01-01T00:00:00'
    assert task_zone_locked_until(config, 'OpsiShop') is None


def test_prevent_overflow_keeps_the_locked_day_delay():
    until = get_server_next_update('00:00')
    stub = PreventStub()
    stub.config.storage[f'{PREVENT_TASK_NAME}.Storage.Storage.ZoneLockedUntil'] = until.isoformat()
    stub.update_prevent_action_point_overflow_schedule(current_ap=50)
    assert stub.config.delayed == [{'target': until, 'task': PREVENT_TASK_NAME}]


def test_prevent_overflow_keeps_minute_schedule_without_lock():
    stub = PreventStub()
    stub.update_prevent_action_point_overflow_schedule(current_ap=50)
    assert 'minute' in stub.config.delayed[0]
    assert stub.config.delayed[0]['task'] == PREVENT_TASK_NAME


def test_os_run_delays_task_on_locked_zone():
    for method in ('opsi_shop', 'opsi_daily', 'opsi_prevent_action_point_overflow'):
        runner = object.__new__(OSCampaignRun)
        runner.config = SimpleNamespace()
        runner.load_campaign = Mock(side_effect=OSZoneLockedError('locked'))
        with patch('module.campaign.os_run.delay_task_for_locked_zone') as delay:
            getattr(OSCampaignRun, method)(runner)
        delay.assert_called_once_with(runner.config)


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
