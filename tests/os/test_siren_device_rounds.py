"""塞壬探测装置处理完之后的收尾（对齐 AzurPilot 的 `map.py:1838-1845`）。

实例日志（2026-10-05 11:00:34 → 11:01:19）暴露的三件事：
1. 装置那一轮自律跑完直接 `return True`，外层 `map_rescan` 一看「本轮已解决一个事件」
   就 `Solved a map event and not in OpsiExplore, stop rescan` 收工，装置旁边剩下的产物
   没人扫，6 秒后就开了新一轮计划作战 —— AP 在这里补了一次 `map_rescan_current` 二次重扫。
2. 自律轮数不看模式：AP 是 探测敌人 3 轮（可指定舰队）/ 探测资源 1 轮 / 柱子已点 0 轮。
3. 弹窗根本没开成（所有舰队都到不了）也照样白跑一轮自律。
"""

from types import SimpleNamespace

from module.exception import MapDetectionError
from module.map.map_base import location2node
from module.os.fixed_patrol import DEVICE_COMPLETED, DEVICE_NONE
from module.os.map import OSMap


class _TempContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DeviceGrid:
    location = (5, 5)
    is_scanning_device = True


class DeviceView:
    """只有 `select(is_scanning_device=True)` 有货；`device_seen` 数的是扫过几遍。"""

    def __init__(self):
        self.device_seen = 0

    def select(self, **kwargs):
        if kwargs.get('is_scanning_device'):
            self.device_seen += 1
            return [DeviceGrid()]
        return []


class DeviceStub:
    """跑真实的 `map_rescan_current` 装置分支（补扫走真递归，靠标记防重入）。"""

    map_rescan_current = OSMap.map_rescan_current
    _mark_event_unreachable = OSMap._mark_event_unreachable
    _siren_device_search_plan = OSMap._siren_device_search_plan

    is_in_task_explore = False

    def __init__(self, walk_result='event', mode='resource', siren_fleet=0,
                 task='OpsiHazard1Leveling', update_fails=False, dialog=True):
        self.view = DeviceView()
        self._solved_map_event = set()
        self._unreachable_event_nodes = set()
        self.states = []
        self.auto_searches = 0
        self.fleets_set = []
        self.patrol_calls = 0
        self.siren_device_mode = mode
        self.is_siren_device_confirmed = False
        self._walk_result = walk_result
        self._dialog = dialog
        self._update_fails = update_fails
        self.current = 1
        self.config = SimpleNamespace(
            task=SimpleNamespace(command=task),
            temporary=lambda **kwargs: _TempContext(),
            cross_get=lambda keys, default=None: (
                siren_fleet if keys.endswith('Siren_Fleet') else default),
        )
        self.device = SimpleNamespace(click=lambda grid: None, screenshot=lambda: None)
        self.fleet_selector = SimpleNamespace(get=lambda: self.current)

    def wait_until_walk_stable(self, **kwargs):
        # 真实现里剧情选项被认成装置剧情才置位（story_skip -> _identify_siren_device_option）
        if self._dialog and 'event' in self._walk_result:
            self.is_siren_device_confirmed = True
        return self._walk_result

    def _goto_scanning_device_with_other_fleets(self, drop=None):
        return False

    def _set_device_state(self, state):
        self.states.append(state)

    def os_auto_search_run(self, drop=None):
        self.auto_searches += 1

    def fleet_set(self, fleet):
        self.current = fleet
        self.fleets_set.append(fleet)

    def update(self):
        if self._update_fails:
            raise MapDetectionError('Image to detect is not in_map')

    def execute_fixed_patrol_scan(self):
        self.patrol_calls += 1


def test_resource_mode_runs_one_round_and_rescans_the_view():
    """探测资源：1 轮自律 + 当前视野补扫一次，产物不会留到下一轮。"""
    stub = DeviceStub(mode='resource')
    assert stub.map_rescan_current() is True
    assert stub.auto_searches == 1
    assert stub.view.device_seen == 2          # 第一遍 + 补扫那一遍
    assert stub._solved_map_event == {'is_scanning_device'}
    assert stub.states[-1] == DEVICE_COMPLETED


def test_extra_rescan_does_not_handle_the_device_twice():
    """补扫必须靠 solved 标记防重入，否则会原地反复点同一个装置。"""
    stub = DeviceStub(mode='resource')
    assert stub.map_rescan_current() is True
    assert stub.auto_searches == 1             # 补扫那一遍没有再跑一轮自律


def test_pillar_needs_no_auto_search_round():
    """信息收集装置/柱子已经主动点完：一轮自律都不该跑，但补扫照做。"""
    stub = DeviceStub(mode='collected')
    assert stub.map_rescan_current() is True
    assert stub.auto_searches == 0
    assert stub.view.device_seen == 2


def test_enemy_mode_runs_three_rounds_with_the_configured_fleet():
    """探测敌人：连跑 3 轮，用配置的舰队，跑完切回原舰队。"""
    stub = DeviceStub(mode='enemy', siren_fleet=3)
    assert stub.map_rescan_current() is True
    assert stub.auto_searches == 3
    assert stub.fleets_set == [3, 1]
    assert stub.view.device_seen == 2


def test_enemy_mode_keeps_current_fleet_when_not_configured():
    stub = DeviceStub(mode='enemy', siren_fleet=0)
    assert stub.map_rescan_current() is True
    assert stub.auto_searches == 3
    assert stub.fleets_set == []


def test_fleet_config_is_read_from_the_leveling_task_when_proxied():
    """代跑别的任务时也要读到侵蚀1 那份 Siren_Fleet，而不是读不到就当 0。"""
    stub = DeviceStub(mode='enemy', siren_fleet=4, task='OpsiDaily')
    assert stub._siren_device_search_plan() == (3, 4)


def test_unreachable_device_runs_no_auto_search_and_no_rescan():
    """所有舰队都到不了：不跑自律、不补扫，标不可达 + 强制移动，并且返回 False。"""
    stub = DeviceStub(walk_result='timeout')
    assert stub.map_rescan_current() is False
    assert stub.auto_searches == 0
    assert stub.view.device_seen == 1          # 没有第二遍
    assert stub.patrol_calls == 1
    assert location2node(DeviceGrid.location) in stub._unreachable_event_nodes
    assert stub.states[-1] == DEVICE_NONE


def test_event_on_the_way_is_not_arrival_at_the_device():
    """路上撞见的是敌人/箱子（walk 也回 'event'）但装置对话没开：不算到达。

    旧判据 `'event' in result` 会在这里当成到位，白跑一轮自律还标成已解决。
    """
    stub = DeviceStub(walk_result='event_combat', dialog=False)
    assert stub.map_rescan_current() is False
    assert stub.auto_searches == 0
    assert stub._solved_map_event == set()
    assert stub.patrol_calls == 1
    assert location2node(DeviceGrid.location) in stub._unreachable_event_nodes


def test_stale_confirmation_does_not_count_as_arrival():
    """上一个装置留下的确认位必须先清掉，否则这一队一点就"到位"。"""
    stub = DeviceStub(walk_result='timeout', dialog=False)
    stub.is_siren_device_confirmed = True
    assert stub.map_rescan_current() is False
    assert stub.auto_searches == 0
    assert stub.patrol_calls == 1


def test_extra_rescan_failure_does_not_break_the_round():
    """补扫时正好撞上黑屏：只跳过这一扫，装置仍算处理完成。"""
    stub = DeviceStub(mode='resource', update_fails=True)
    assert stub.map_rescan_current() is True
    assert stub.auto_searches == 1
    assert stub.view.device_seen == 1
    assert stub.states[-1] == DEVICE_COMPLETED
