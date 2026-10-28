"""每日任务的迷雾侦察（对齐 AzurPilot）。

覆盖：
1. 「本月开荒已完成」的判断（必须带本月标记，防上月残留）
2. 侦察的六个前置条件
3. 钩子里「先侦察、再清图」的顺序
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.config.utils import get_os_next_reset
from module.os.map import OSMap
from module.os.tasks.daily import OpsiDaily

THIS_MONTH = get_os_next_reset().isoformat()
LAST_MONTH = '2020-01-01T00:00:00'


def _make(task='OpsiDaily', progress='已完成百分之100.00', reset=THIS_MONTH,
          is_port=False, name_hidden=False, special=False):
    config = SimpleNamespace(
        OpsiExplore_ExploreProgress=progress,
        OpsiExplore_MeowfficerCleanupState={'reset': reset, 'phase': 'done'},
        task=SimpleNamespace(command=task),
    )
    stub = SimpleNamespace(
        config=config,
        zone=SimpleNamespace(is_port=is_port),
        is_zone_name_hidden=name_hidden,
        is_in_special_zone=lambda: special,
    )
    # 桩对象不带类方法，手动挂上真实实现
    stub._os_monthly_explore_complete = lambda: OpsiDaily._os_monthly_explore_complete(stub)
    return stub


# ------------------------------------------------- 本月开荒是否完成

def test_explore_complete_requires_this_month_marker():
    stub = _make(progress='已完成百分之100.00', reset=THIS_MONTH)
    assert OpsiDaily._os_monthly_explore_complete(stub) is True


def test_explore_complete_false_when_marker_from_last_month():
    """上月留下的 100% 不能当成本月已完成，否则新月头几天不扫雾。"""
    stub = _make(progress='已完成百分之100.00', reset=LAST_MONTH)
    assert OpsiDaily._os_monthly_explore_complete(stub) is False


def test_explore_complete_false_when_progress_not_full():
    stub = _make(progress='已完成百分之42.86', reset=THIS_MONTH)
    assert OpsiDaily._os_monthly_explore_complete(stub) is False


def test_explore_complete_false_when_state_missing():
    stub = _make()
    stub.config.OpsiExplore_MeowfficerCleanupState = None
    assert OpsiDaily._os_monthly_explore_complete(stub) is False


# ----------------------------------------------------- 侦察前置条件

def test_needs_recon_scan_in_unexplored_normal_zone():
    stub = _make(progress='已完成百分之12.00', reset=THIS_MONTH)
    assert OpsiDaily._os_daily_needs_recon_scan(stub) is True


def test_needs_recon_scan_false_for_other_tasks():
    stub = _make(task='OpsiArchive', progress='已完成百分之12.00')
    assert OpsiDaily._os_daily_needs_recon_scan(stub) is False


def test_needs_recon_scan_false_in_port():
    stub = _make(is_port=True, progress='已完成百分之12.00')
    assert OpsiDaily._os_daily_needs_recon_scan(stub) is False


def test_needs_recon_scan_false_in_safe_zone():
    stub = _make(name_hidden=True, progress='已完成百分之12.00')
    assert OpsiDaily._os_daily_needs_recon_scan(stub) is False


def test_needs_recon_scan_false_in_special_zone():
    stub = _make(special=True, progress='已完成百分之12.00')
    assert OpsiDaily._os_daily_needs_recon_scan(stub) is False


def test_needs_recon_scan_false_when_explore_done():
    stub = _make(progress='已完成百分之100.00', reset=THIS_MONTH)
    assert OpsiDaily._os_daily_needs_recon_scan(stub) is False


# --------------------------------------------------------- 钩子顺序

def test_run_first_auto_search_scans_before_clearing():
    """需要侦察时：先下侦察指令，再走父类的清图。"""
    calls = []
    original = OSMap.run_first_auto_search
    OSMap.run_first_auto_search = lambda self: calls.append('super')
    try:
        stub = object.__new__(OpsiDaily)
        stub._os_daily_needs_recon_scan = lambda: True
        stub.os_order_execute = lambda **kwargs: calls.append(('recon', kwargs))
        OpsiDaily.run_first_auto_search(stub)
    finally:
        OSMap.run_first_auto_search = original

    assert calls == [('recon', {'recon_scan': True, 'submarine_call': False}), 'super'], calls


def test_run_first_auto_search_skips_scan_when_not_needed():
    calls = []
    original = OSMap.run_first_auto_search
    OSMap.run_first_auto_search = lambda self: calls.append('super')
    try:
        stub = object.__new__(OpsiDaily)
        stub._os_daily_needs_recon_scan = lambda: False
        stub.os_order_execute = lambda **kwargs: calls.append(('recon', kwargs))
        OpsiDaily.run_first_auto_search(stub)
    finally:
        OSMap.run_first_auto_search = original

    assert calls == ['super'], calls


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
