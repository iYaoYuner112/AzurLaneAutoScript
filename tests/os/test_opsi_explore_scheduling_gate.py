"""开荒期间不运行智能调度（对齐 AzurPilot 的三层拦截）。

覆盖：
1. `is_smart_scheduling_enabled()` 在开荒期间返回 False
2. `opsi_scheduling()` 在 load_campaign() 之前就延期并返回
3. 开荒推后名单包含 OpsiScheduling
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.campaign.os_run import OSCampaignRun
from module.os.tasks.explore import OpsiExplore
from module.os_handler.os_status import OSStatus


# ------------------------------------------------ 1. 开关合流

def _status_stub(in_explore, enabled):
    return SimpleNamespace(
        is_in_opsi_explore=lambda: in_explore,
        config=SimpleNamespace(is_task_enabled=lambda task: enabled),
    )


def test_scheduling_disabled_while_exploring():
    stub = _status_stub(in_explore=True, enabled=True)
    assert OSStatus.is_smart_scheduling_enabled.fget(stub) is False


def test_scheduling_follows_switch_when_explore_done():
    enabled = OSStatus.is_smart_scheduling_enabled.fget
    assert enabled(_status_stub(False, True)) is True
    assert enabled(_status_stub(False, False)) is False


# ------------------------------------------------ 2. 入口前置拦截

def _run_stub(in_explore):
    calls = {'load': 0, 'delay': [], 'scheduling': 0}

    def load_campaign(cls=None):
        calls['load'] += 1
        return SimpleNamespace(os_scheduling=lambda: calls.__setitem__('scheduling', calls['scheduling'] + 1))

    return SimpleNamespace(
        is_in_opsi_explore=lambda: in_explore,
        load_campaign=load_campaign,
        config=SimpleNamespace(
            cross_get=lambda keys, default=None: '04:00',
            task_delay=lambda **kwargs: calls['delay'].append(kwargs),
        ),
    ), calls


def test_scheduling_defers_before_loading_campaign():
    """开荒中：不加载战役（否则 os_init 会打一场），直接延期到本任务的服务器刷新。"""
    stub, calls = _run_stub(in_explore=True)
    OSCampaignRun.opsi_scheduling(stub)

    assert calls['load'] == 0, '不应该调用 load_campaign()'
    assert calls['scheduling'] == 0
    assert calls['delay'] == [{'server_update': '04:00', 'task': 'OpsiScheduling'}], calls['delay']


def test_scheduling_runs_normally_when_explore_done():
    stub, calls = _run_stub(in_explore=False)
    OSCampaignRun.opsi_scheduling(stub)

    assert calls['load'] == 1
    assert calls['scheduling'] == 1
    assert calls['delay'] == []


# ------------------------------------------------ 3. 开荒推后名单

def test_explore_delay_list_includes_scheduling():
    written = {}

    class _Config:
        Scheduler_NextRun = '2026-11-02 00:00:00'

        def multi_set(self):
            class _Ctx:
                def __enter__(self_inner):
                    return None

                def __exit__(self_inner, *exc):
                    return False
            return _Ctx()

        def cross_get(self, keys, default=None):
            return '2000-01-01 00:00:00'

        def cross_set(self, keys, value):
            written[keys.split('.')[0]] = value

    stub = SimpleNamespace(config=_Config())
    OpsiExplore._os_explore_task_delay(stub)

    assert 'OpsiScheduling' in written, f'推迟名单缺 OpsiScheduling: {sorted(written)}'
    assert 'OpsiHazard1Leveling' in written, '侵蚀1 仍应被推迟（我们没搬 AllowHazard1Leveling 开关）'


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
