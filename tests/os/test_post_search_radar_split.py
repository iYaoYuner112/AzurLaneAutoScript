"""计划作战结束后"主队清问号"的分工（按 AzurPilot 的口径收窄，但保留我们的兜底）。

AP 的 d784de8fc 把战后的逐队扫雷达统一交给强制移动，理由是两个流程并存会把同一批雷达扫
两遍；代价是他们自己写在提交里的副作用——强制移动关掉时干脆不扫雷达了。我们的取向：

- 强制移动开着（同轮里一定会逐队扫，而且都从主队开始）→ 重扫前不再读一遍主队雷达；
- 强制移动关着 → 保留重扫前的主队清问号，这是 AP 有意放弃的那层收益。

顺序仍然重要：开着时若重扫先找到事件，强制移动就不跑了，问号留给下一轮——这是 AP 认的取舍
（省下的是行走的行动力和 15~25 秒），不是漏改。
"""

from types import SimpleNamespace

from module.config.config import TaskEnd
from module.os.map import OSMap


class SearchStub:
    """`run_strategic_search()` 需要的对象，按调用顺序记录它到底做了什么。"""

    _primary_radar_swept_later = OSMap._primary_radar_swept_later

    def __init__(self, meowfficer=False, cl1_patrol=False, meow_patrol=False, error=None):
        self._meowfficer = meowfficer
        self._cl1_patrol = cl1_patrol
        self._meow_patrol = meow_patrol
        self._error = error
        self.calls = []
        self._solved_map_event = {'is_akashi'}
        self._solved_fleet_mechanism = True
        self.hp_reads = 0

    def _is_meowfficer_task(self):
        return self._meowfficer

    def _forced_move_enabled(self):
        return self._cl1_patrol

    def _meowfficer_patrol_enabled(self):
        return self._meow_patrol

    def handle_ash_beacon_attack(self):
        pass

    def os_auto_search_run(self, strategic=False):
        assert strategic is True
        self.calls.append('search')
        if self._error is not None:
            raise self._error

    def hp_reset(self):
        pass

    def hp_get(self):
        self.hp_reads += 1

    def clear_question(self, drop=None):
        self.calls.append('clear_question')

    def map_rescan(self, rescan_mode='full', drop=None):
        self.calls.append('map_rescan')
        return False


def run(**kwargs):
    stub = SearchStub(**kwargs)
    result = OSMap.run_strategic_search(stub)
    return stub, result


def test_cl1_with_patrol_on_skips_the_duplicate_primary_clear():
    """同轮里 L0 一定从主队开始扫雷达，重扫前就不必再读一遍。"""
    stub, result = run(cl1_patrol=True)
    assert stub.calls == ['search', 'map_rescan'], stub.calls
    assert result is True


def test_cl1_with_patrol_off_still_clears_primary_first():
    """没有强制移动兜底时，主队清问号照旧跑，而且排在重扫之前。"""
    stub, result = run(cl1_patrol=False)
    assert stub.calls == ['search', 'clear_question', 'map_rescan'], stub.calls
    assert result is True


def test_meowfficer_uses_its_own_switch():
    """短猫看的是它自己的强制移动开关，不是侵蚀1 那个。"""
    stub, _ = run(meowfficer=True, meow_patrol=True, cl1_patrol=False)
    assert stub.calls == ['search', 'map_rescan'], stub.calls
    stub, _ = run(meowfficer=True, meow_patrol=False, cl1_patrol=True)
    assert stub.calls == ['search', 'clear_question', 'map_rescan'], stub.calls


def test_event_flags_are_reset_before_the_scan():
    """上一轮的处理标记要在重扫前清掉，否则早停判定会误触发。"""
    stub, _ = run(cl1_patrol=True)
    assert stub._solved_map_event == set()
    assert stub._solved_fleet_mechanism is False
    assert stub.hp_reads == 1


def test_interrupted_search_still_scans_and_returns_false():
    """搜索被意外错误打断：这轮的扫描不能跳过，但要把"被打断"告诉调用方。"""
    stub, result = run(cl1_patrol=True, error=ValueError('boom'))
    assert stub.calls == ['search', 'map_rescan'], stub.calls
    assert result is False

    stub, result = run(cl1_patrol=False, error=ValueError('boom'))
    assert stub.calls == ['search', 'clear_question', 'map_rescan'], stub.calls
    assert result is False


def test_task_switch_still_propagates():
    """切任务的 TaskEnd 不属于"被打断"，必须原样上抛，不能接着扫图。"""
    stub = SearchStub(cl1_patrol=True, error=TaskEnd('switch'))
    raised = False
    try:
        OSMap.run_strategic_search(stub)
    except TaskEnd:
        raised = True
    assert raised
    assert stub.calls == ['search'], stub.calls


class SwitchStub:
    """`_meowfficer_patrol_enabled()` 需要的配置读取。"""

    _meowfficer_patrol_enabled = OSMap._meowfficer_patrol_enabled

    def __init__(self, value=None, cross_value=False):
        attrs = {} if value is None else {'OpsiMeowfficerFarming_ExecuteFixedPatrolScan': value}
        self.config = SimpleNamespace(cross_get=lambda keys, default=None: cross_value, **attrs)


def test_meowfficer_switch_reads_both_paths():
    assert SwitchStub(value=True)._meowfficer_patrol_enabled() is True
    assert SwitchStub(value=False)._meowfficer_patrol_enabled() is False
    # 配置里没有这个键时退回 cross_get
    assert SwitchStub(value=None, cross_value=True)._meowfficer_patrol_enabled() is True
    assert SwitchStub(value=None, cross_value=False)._meowfficer_patrol_enabled() is False
