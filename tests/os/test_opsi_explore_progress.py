"""每月开荒进度记录（纯记录字段，对齐 AzurPilot）。

只验证写入内容的格式与边界，不涉及流程。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.os.tasks.explore import OpsiExplore


def _make_stub():
    written = {}

    class _Config:
        def __setattr__(self, key, value):
            if key.startswith('OpsiExplore_'):
                written[key] = value
            object.__setattr__(self, key, value)

    return SimpleNamespace(config=_Config()), written


def test_progress_written_as_percentage_with_two_decimals():
    stub, written = _make_stub()
    OpsiExplore._os_explore_record_progress(stub, 3, 7)
    assert written['OpsiExplore_ExploreProgress'] == '已完成百分之42.86', written


def test_progress_none_when_nothing_done():
    stub, written = _make_stub()
    OpsiExplore._os_explore_record_progress(stub, 0, 9)
    assert written['OpsiExplore_ExploreProgress'] == '已完成百分之0.00', written


def test_progress_skipped_when_no_zone_configured():
    stub, written = _make_stub()
    OpsiExplore._os_explore_record_progress(stub, 1, 0)
    assert written == {}, f'没有海域时不应写进度: {written}'


def test_full_progress_reaches_hundred():
    stub, written = _make_stub()
    OpsiExplore._os_explore_record_progress(stub, 5, 5)
    assert written['OpsiExplore_ExploreProgress'] == '已完成百分之100.00', written


if __name__ == '__main__':
    test_progress_written_as_percentage_with_two_decimals()
    test_progress_none_when_nothing_done()
    test_progress_skipped_when_no_zone_configured()
    test_full_progress_reaches_hundred()
    print('all passed')
