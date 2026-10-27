"""短猫「没有可打海域」的收尾方式（回归：不要把整条调度停到明天）。

代理执行时应当只报 NO_CONTENT（调度换下一个候选），独立运行才保持
`task_delay(server_update=True) + task_stop()`。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import module.os.tasks.meowfficer_farming as mf
from module.config.config import TaskEnd
from module.os.tasks.task_context import OpsiNoContent, OpsiTaskContext


class _EmptyZones:
    """空海域集合：delete/select/sort 都返回自己，布尔值为 False。"""

    location = (0, 0)

    def delete(self, *_a, **_k):
        return self

    def select(self, *_a, **_k):
        return self

    def sort_by_clock_degree(self, *_a, **_k):
        return self

    def __bool__(self):
        return False


class _FakeConfig:
    OpsiMeowfficerFarming_HazardLevel = 6

    def __init__(self):
        self.delays = []
        self.stopped = False
        self.task = None

    def task_delay(self, **kwargs):
        self.delays.append(kwargs)

    def task_stop(self):
        self.stopped = True
        raise TaskEnd


class _Stub:
    def __init__(self, config):
        self.config = config
        self.zone = _EmptyZones()
        self.zones = _EmptyZones()

    def zone_select(self, **_kwargs):
        return _EmptyZones()


def _run(mode):
    """在指定模式下跑一次「没有可打海域」的分支。"""
    config = _FakeConfig()
    if mode == 'proxy':
        config._opsi_context = OpsiTaskContext(
            parent_task='OpsiScheduling', current_task='OpsiMeowfficerFarming')
    original = mf.SelectedGrids
    mf.SelectedGrids = lambda *a, **k: _EmptyZones()
    try:
        return config, mf.OpsiMeowfficerFarming._meow_handle_normal_search(_Stub(config))
    finally:
        mf.SelectedGrids = original


def test_proxied_no_zone_reports_no_content_instead_of_stopping_scheduler():
    """代理执行：抛 OpsiNoContent，且不写 task_delay / 不 task_stop。"""
    config = _FakeConfig()
    config._opsi_context = OpsiTaskContext(
        parent_task='OpsiScheduling', current_task='OpsiMeowfficerFarming')
    original = mf.SelectedGrids
    mf.SelectedGrids = lambda *a, **k: _EmptyZones()
    try:
        try:
            mf.OpsiMeowfficerFarming._meow_handle_normal_search(_Stub(config))
        except OpsiNoContent:
            pass
        else:
            raise AssertionError('代理执行时应抛 OpsiNoContent')
    finally:
        mf.SelectedGrids = original

    assert config.delays == [], f'代理执行不应写延迟，实际: {config.delays}'
    assert config.stopped is False, '代理执行不应 task_stop'
    assert config._opsi_no_content_task == 'OpsiMeowfficerFarming'


def test_standalone_no_zone_keeps_legacy_delay_and_stop():
    """独立运行：保持 task_delay(server_update=True) + task_stop 的老行为。"""
    config = _FakeConfig()
    original = mf.SelectedGrids
    mf.SelectedGrids = lambda *a, **k: _EmptyZones()
    try:
        try:
            mf.OpsiMeowfficerFarming._meow_handle_normal_search(_Stub(config))
        except TaskEnd:
            pass
        else:
            raise AssertionError('独立运行时应抛 TaskEnd')
    finally:
        mf.SelectedGrids = original

    assert config.delays == [{'server_update': True}], f'实际延迟: {config.delays}'
    assert config.stopped is True


if __name__ == '__main__':
    test_proxied_no_zone_reports_no_content_instead_of_stopping_scheduler()
    test_standalone_no_zone_keeps_legacy_delay_and_stop()
    print('all passed')
