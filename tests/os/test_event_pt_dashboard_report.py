"""活动 PT 的上报时机（总览页资源卡）。

实机问题（2026-11-01）：默认 `EventGeneral.PtLimit = 0`，旧实现在 limit<=0 时直接
return，`get_event_pt()` 从不执行 → 总览页的活动 PT 永远停在旧值、刷活动图也不更新。
AzurPilot 在 limit<=0 时仍会读一次，这里对齐。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.campaign.campaign_event import CampaignEvent


def _stub(limit, command='Event', stage_main=False, pt=1234):
    calls = {'get': 0, 'disable': 0}

    def get_event_pt():
        calls['get'] += 1
        return pt

    def disable_tasks(tasks):
        calls['disable'] += 1

    stub = SimpleNamespace(
        config=SimpleNamespace(
            EventGeneral_PtLimit=limit,
            Scheduler_Command=command,
            Campaign_Name='event_20261022_cn',
        ),
        stage_is_main=lambda name: stage_main,
        get_event_pt=get_event_pt,
        _disable_tasks=disable_tasks,
    )
    return stub, calls


def test_pt_read_when_no_limit_configured():
    """没设上限（默认 0）时仍要读一次，否则总览页永不刷新。"""
    stub, calls = _stub(limit=0)
    assert CampaignEvent.event_pt_limit_triggered(stub) is False
    assert calls['get'] == 1, '没有读活动 PT'


def test_pt_limit_still_works_when_configured():
    stub, calls = _stub(limit=5000, pt=6000)
    assert CampaignEvent.event_pt_limit_triggered(stub) is True
    assert calls['disable'] == 1

    stub, calls = _stub(limit=5000, pt=1000)
    assert CampaignEvent.event_pt_limit_triggered(stub) is False
    assert calls['get'] == 1


def test_no_read_outside_event_pages():
    stub, calls = _stub(limit=0, command='Daily')
    assert CampaignEvent.event_pt_limit_triggered(stub) is False
    assert calls['get'] == 0, '非活动任务不该 OCR 活动 PT'


def test_no_read_for_main_campaign_farming():
    """主线打捞在普通战役页，右上角不是活动 PT 区域。"""
    stub, calls = _stub(limit=0, command='GemsFarming', stage_main=True)
    assert CampaignEvent.event_pt_limit_triggered(stub) is False
    assert calls['get'] == 0


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
