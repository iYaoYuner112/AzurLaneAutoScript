"""Opsi 任务被其他任务抢占后的地图状态恢复（Resume Barrier）单测。

核心原则：只要 Opsi 任务被真正抢占过，恢复时就不能再信任抢占前的地图缓存，
必须走一次明确的恢复屏障——FULL RESCAN + 失效旧 solved 状态 + 清除 stale 标记。

覆盖：
- task_switched 只在「Opsi 任务被切走」时标记地图 stale；
- task_switched 额外在「OpsiScheduling 被切走」时留下一次性恢复标志；
- resume barrier 只做一次 FULL RESCAN，并失效旧 cache、清标记；
- OpsiScheduling 恢复走探测路径：先试一次自律寻敌，没效果才回退 FULL RESCAN；
- 标记清除后不会反复触发 rescan（防每个循环都扫 / 防连续两次抢占重复扫）。
"""

from datetime import datetime

from types import SimpleNamespace

from module.config.config import (
    AzurLaneConfig, Function, OS_MAP_STALE_KEY, OS_RESUME_RECOVERY_KEY
)
from module.exception import RequestHumanTakeover
from module.os.fixed_patrol import DEVICE_NONE
from module.os.map import OSMap


def make_function(command):
    return Function({
        'Scheduler': {
            'Command': command,
            'Enable': True,
            'NextRun': datetime(2026, 1, 1),
        }
    })


class FakeConfig:
    """只提供 task_switched 需要的属性。"""

    def __init__(self, prev_command, next_command):
        self.stop_event = None
        self.task = make_function(prev_command)
        self.next = make_function(next_command)
        self.cross_set_calls = []

    def load(self):
        pass

    def get_next(self):
        return self.next

    def cross_set(self, key, value):
        self.cross_set_calls.append((key, value))


# ---- task_switched 标记 stale ----

def test_switch_away_from_opsi_marks_map_stale():
    cfg = FakeConfig('OpsiScheduling', 'Commission')
    assert AzurLaneConfig.task_switched(cfg) is True
    assert (OS_MAP_STALE_KEY, True) in cfg.cross_set_calls


def test_switch_away_from_non_opsi_does_not_mark():
    cfg = FakeConfig('Commission', 'Research')
    assert AzurLaneConfig.task_switched(cfg) is True
    assert cfg.cross_set_calls == []


def test_continue_task_does_not_mark():
    cfg = FakeConfig('OpsiScheduling', 'OpsiScheduling')
    assert AzurLaneConfig.task_switched(cfg) is False
    assert cfg.cross_set_calls == []


# ---- task_switched 标记一次性恢复标志（OpsiScheduling 专属）----

def test_switch_away_from_opsi_scheduling_marks_resume_recovery():
    cfg = FakeConfig('OpsiScheduling', 'Commission')
    assert AzurLaneConfig.task_switched(cfg) is True
    assert (OS_RESUME_RECOVERY_KEY, True) in cfg.cross_set_calls


def test_switch_away_from_other_opsi_does_not_mark_resume_recovery():
    cfg = FakeConfig('OpsiDaily', 'Commission')
    assert AzurLaneConfig.task_switched(cfg) is True
    assert all(key != OS_RESUME_RECOVERY_KEY for key, _ in cfg.cross_set_calls)


def test_task_switched_uses_owner_when_a_child_is_proxied():
    """代理子任务期间必须以拥有者（调度器）做切换判断。

    `config.task` 此刻是子任务，直接拿它比较会把每一轮都误判成任务切换。
    """
    cfg = FakeConfig('OpsiHazard1Leveling', 'OpsiScheduling')
    cfg._task_switch_owner = make_function('OpsiScheduling')
    assert AzurLaneConfig.task_switched(cfg) is False
    assert cfg.cross_set_calls == []


def test_task_switched_without_owner_would_mistake_the_child_for_a_switch():
    """没有拥有者信息时，子任务身份确实会被当作切换——这正是要守护的契约。"""
    cfg = FakeConfig('OpsiHazard1Leveling', 'OpsiScheduling')
    assert AzurLaneConfig.task_switched(cfg) is True


# ---- resume barrier ----

class StatefulConfig:
    """记录 stale / recovery 标记的配置替身。"""

    def __init__(self, stale=False, command='OpsiScheduling', recovery=False):
        self.task = SimpleNamespace(command=command)
        self.store = {
            OS_MAP_STALE_KEY: stale,
            OS_RESUME_RECOVERY_KEY: recovery,
        }

    def cross_get(self, key, default=False):
        return self.store.get(key, default)

    def cross_set(self, key, value):
        self.store[key] = value

    @property
    def stale(self):
        return self.store.get(OS_MAP_STALE_KEY, False)

    @property
    def recovery(self):
        return self.store.get(OS_RESUME_RECOVERY_KEY, False)


def make_resume_stub(stale):
    stub = SimpleNamespace(
        config=StatefulConfig(stale),
        zone=SimpleNamespace(zone_id=22),
        _solved_map_event={'is_akashi'},
        _solved_fleet_mechanism=True,
        rescan_calls=[],
        map_rescan=lambda **kw: None,
    )
    # Bind the real OSMap helpers used by ensure_map_state_current().
    stub._os_map_was_interrupted = lambda: OSMap._os_map_was_interrupted(stub)
    stub._os_resume_recovery_available = lambda: OSMap._os_resume_recovery_available(stub)
    stub._device_state = DEVICE_NONE
    return stub


def make_probe_stub(stale=True, recovery=True, command='OpsiScheduling', zone_id=10):
    """带探测所需方法的 stub：恢复探测（先寻敌、没效果才重扫）。"""
    stub = make_resume_stub(stale)
    stub.config = StatefulConfig(stale, command=command, recovery=recovery)
    stub.zone = SimpleNamespace(zone_id=zone_id)
    stub.probe_calls = []
    stub._os_resume_probe_pending = False
    stub._os_resume_recovery_finished = (
        lambda: OSMap._os_resume_recovery_finished(stub))

    def run_auto_search(**kwargs):
        stub.probe_calls.append(kwargs)
        return stub.probe_result

    stub.run_auto_search = run_auto_search
    stub.probe_result = 0
    stub.on_auto_search_battle_count_reset = lambda: None
    stub._auto_search_battle_count = 0
    return stub


def test_os_map_was_interrupted_reads_flag():
    assert OSMap._os_map_was_interrupted(make_resume_stub(True)) is True
    assert OSMap._os_map_was_interrupted(make_resume_stub(False)) is False


def test_ensure_map_state_current_does_full_rescan_and_invalidates():
    stub = make_resume_stub(True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    assert stub._solved_map_event == set()
    assert stub._solved_fleet_mechanism is False


def test_ensure_map_state_current_clears_stale_flag():
    stub = make_resume_stub(True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.config.stale is False


def test_resume_barrier_runs_once_per_interruption():
    """恢复屏障只做一次：清了标记后，后续进入不会再触发 rescan。"""
    stub = make_resume_stub(True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert len(stub.rescan_calls) == 1
    # 标记已清除，第二次进入不会再次 FULL RESCAN
    assert OSMap._os_map_was_interrupted(stub) is False


# ---- 智能调度恢复：先探测自律寻敌，没效果才回退重扫 ----

def test_ensure_defers_rescan_to_probe_on_scheduling_resume():
    """OpsiScheduling 被抢占后恢复：不立即重扫，交给首次自律寻敌探测。"""
    stub = make_probe_stub(stale=True, recovery=True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    # 不重扫，等待 os_init 里的探测
    assert stub.rescan_calls == []
    # 旧缓存失效
    assert stub._solved_map_event == set()
    assert stub._solved_fleet_mechanism is False
    # 一次性标志已消费，探测挂起，stale 留给探测结果清理
    assert stub.config.recovery is False
    assert stub._os_resume_probe_pending is True
    assert stub.config.stale is True


def test_ensure_ash_beacon_zone_falls_back_to_full_rescan():
    """灰烬信标海域（22/44/154）没有探测点，保持原有直接重扫行为。"""
    stub = make_probe_stub(stale=True, recovery=True, zone_id=22)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    assert stub.config.stale is False
    assert stub.config.recovery is False
    assert stub._os_resume_probe_pending is False


def test_ensure_ignores_recovery_flag_for_other_tasks():
    """恢复标志只在 OpsiScheduling 上生效；其他任务恢复走原有全图重扫。"""
    stub = make_probe_stub(stale=True, recovery=True, command='OpsiDaily')
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    assert stub.config.stale is False
    # 非智能调度恢复不消费 recovery 标志
    assert stub.config.recovery is True


def test_ensure_full_rescan_without_recovery_flag():
    """普通 Opsi 抢占恢复（无 recovery 标志）：维持原有无条件重扫。"""
    stub = make_probe_stub(stale=True, recovery=False)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    assert stub._os_resume_probe_pending is False


# ---- 探测方法：_os_resume_recovery_auto_search ----

def test_probe_started_skips_rescan_and_clears_stale():
    """自律寻敌正常运作：地图状态视为有效，跳过全图重扫。"""
    stub = make_probe_stub()
    stub.probe_result = 3
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap._os_resume_recovery_auto_search(stub)
    assert stub.probe_calls == [
        {'question': False, 'rescan': False, 'after_auto_search': False}
    ]
    assert stub.rescan_calls == []
    assert stub.config.stale is False
    assert stub._os_resume_probe_pending is False


def test_probe_normal_finish_without_combat_counts_as_started():
    """寻敌正常跑完但 0 战斗（海域已清）：自动搜索机制正常工作过，同样算开始。"""
    stub = make_probe_stub()
    stub.probe_result = 0
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap._os_resume_recovery_auto_search(stub)
    assert stub.rescan_calls == []
    assert stub.config.stale is False


def test_probe_no_effect_falls_back_to_full_rescan():
    """自动搜索无法开始（RequestHumanTakeover）：回退一次全图重扫。"""
    stub = make_probe_stub()
    stub.probe_result = 0

    def run_auto_search_fail(**kwargs):
        stub.probe_calls.append(kwargs)
        raise RequestHumanTakeover('Unable to use auto search')

    stub.run_auto_search = run_auto_search_fail
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap._os_resume_recovery_auto_search(stub)
    assert len(stub.probe_calls) == 1
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    # 回退重扫前再次失效探测期间产生的旧缓存
    assert stub._solved_map_event == set()
    assert stub._solved_fleet_mechanism is False
    assert stub.config.stale is False


def test_probe_is_one_shot():
    """探测标志一次性：探测完成后置 False，不会在下个循环再次触发。"""
    stub = make_probe_stub()
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap._os_resume_recovery_auto_search(stub)
    assert stub._os_resume_probe_pending is False


def test_recovery_available_requires_scheduling_command():
    """恢复标志检查：必须是 OpsiScheduling 且标志存在。"""
    stub = make_probe_stub(recovery=True, command='OpsiScheduling')
    assert OSMap._os_resume_recovery_available(stub) is True
    stub = make_probe_stub(recovery=True, command='OpsiDaily')
    assert OSMap._os_resume_recovery_available(stub) is False
    stub = make_probe_stub(recovery=False, command='OpsiScheduling')
    assert OSMap._os_resume_recovery_available(stub) is False


# ---- 验收场景 ----

def test_case1_no_interruption_adds_no_probe_and_no_rescan():
    """Case 1：OpsiScheduling 没被抢占（无 stale）→ 不探测、不额外重扫。"""
    stub = make_probe_stub(stale=False, recovery=False)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap.ensure_map_state_current(stub)
    assert stub.rescan_calls == []
    assert stub.probe_calls == []
    assert stub._os_resume_probe_pending is False


def test_case3_probe_runs_at_most_once_per_resume():
    """Case 3/11：一次抢占恢复只探测一次；探测成功后第二次 ensure 不再挂起。"""
    stub = make_probe_stub(stale=True, recovery=True)
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    # 恢复入口：挂起探测并消费一次性标志
    OSMap.ensure_map_state_current(stub)
    assert stub._os_resume_probe_pending is True
    assert stub.config.recovery is False
    # 第一次探测：自律真正启动 → 不重扫
    stub.probe_result = 2
    OSMap._os_resume_recovery_auto_search(stub)
    assert len(stub.probe_calls) == 1
    assert stub.rescan_calls == []
    assert stub._os_resume_probe_pending is False
    # 再次进入 ensure：stale 已清 + 标志已消费 → 完全不动作
    OSMap.ensure_map_state_current(stub)
    assert stub._os_resume_probe_pending is False
    assert stub.rescan_calls == []
    assert len(stub.probe_calls) == 1


def test_case4_no_effect_triggers_exactly_one_rescan():
    """Case 4：第一次自律无反应 → 只探测一次 + 只重扫一次，绝不重复点击。"""
    stub = make_probe_stub()
    stub.probe_result = 0

    def run_auto_search_fail(**kwargs):
        stub.probe_calls.append(kwargs)
        raise RequestHumanTakeover('Unable to use auto search')

    stub.run_auto_search = run_auto_search_fail
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap._os_resume_recovery_auto_search(stub)
    assert len(stub.probe_calls) == 1
    assert stub.rescan_calls == [{'rescan_mode': 'full'}]
    assert stub.config.stale is False


def test_case5_rescan_invalidates_stale_event_cache():
    """Case 5/6：恢复重扫前失效旧事件缓存，避免沿用抢占前的旧地图数据。"""
    stub = make_probe_stub()
    stub._solved_map_event = {'is_akashi', 'is_scanning_device'}
    stub._solved_fleet_mechanism = True
    stub.probe_result = 0

    def run_auto_search_fail(**kwargs):
        stub.probe_calls.append(kwargs)
        raise RequestHumanTakeover('Unable to use auto search')

    stub.run_auto_search = run_auto_search_fail
    stub.map_rescan = lambda **kw: stub.rescan_calls.append(kw)
    OSMap._os_resume_recovery_auto_search(stub)
    assert stub._solved_map_event == set()
    assert stub._solved_fleet_mechanism is False
