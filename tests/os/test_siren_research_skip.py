"""塞壬研究装置关闭时，必须在「点击 / 行军之前」就跳过（对齐 AzurPilot）。

对比基线：wess09/AzurPilot dev `0cb356f6`（2026-10-08；本地镜像 `88aebca6e`，map.py md5 相同）。
- AP 两处都在动作之前判定：`clear_question` 用 `_should_skip_siren_research(grid)`
  （map.py:1331-1334），`map_rescan_current` 用 `_is_siren_research_enabled`（map.py:1746-1749）。
- 我们原来两处都没有预检查：会点过去、走过去、开对话。而「选离开」同样会把
  `is_siren_device_confirmed` 置 True（info_handler 在 `_identify_siren_device_option`
  返回非 None 时置位），于是 `clear_question` 把这次当成「装置已处理」，`siren_mode`
  为 None 落到 else 分支，**多跑一轮自律寻敌**；`map_rescan_current` 同样多跑一轮。
- 关闭功能时用户要的是「别碰它」，所以判定必须发生在任何动作之前。

`record_siren_research_device` 没有一起搬：那是 AP `statistics/cl1_database` 的统计写入
（AP 30 个统计文件 vs 我们 9 个），纯记录，对行为零影响。
"""

from types import SimpleNamespace

from module.os.fixed_patrol import DEVICE_COMPLETED, DEVICE_DIALOG_OPEN, DEVICE_TARGETED
from module.os.map import OSMap


class _TempContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DeviceGrid:
    location = (5, 5)
    is_scanning_device = True


class PlainGrid:
    location = (1, 1)
    is_scanning_device = False
    is_logging_tower = False
    str = 'B2'


class FleetSeen:
    count = 1


class _Config:
    """只实现 `_is_siren_research_enabled` / `_siren_device_search_plan` 需要的部分。"""

    def __init__(self, task='OpsiHazard1Leveling', research_enable=True,
                 has_key=True, siren_fleet=0):
        self.task = SimpleNamespace(command=task)
        self._research_enable = research_enable
        self._has_key = has_key
        self._siren_fleet = siren_fleet
        self.queried = []

    def temporary(self, **kwargs):
        return _TempContext()

    def cross_get(self, keys, default=None):
        self.queried.append(keys)
        if keys.endswith('SirenResearch_Enable'):
            return self._research_enable if self._has_key else default
        if keys.endswith('Siren_Fleet'):
            return self._siren_fleet
        return default


class GateStub:
    """只带两个判定函数；必须挂成类属性，property 才能正常生效。"""

    _is_siren_research_enabled = OSMap._is_siren_research_enabled
    _should_skip_siren_research = OSMap._should_skip_siren_research

    def __init__(self, config=None):
        self.config = config or _Config()


# ---------------------------------------------------------------- 判定函数本身

def test_enabled_by_default_when_the_key_is_missing():
    """老 alas.json 里没有 OpsiSirenBug 组时按生成的默认值 True，
    否则舰队已经走到装置前却因为读不到开关白跑一趟。"""
    stub = GateStub(_Config(has_key=False))
    assert stub._is_siren_research_enabled is True


def test_disabled_when_configured_off():
    stub = GateStub(_Config(research_enable=False))
    assert stub._is_siren_research_enabled is False


def test_other_tasks_read_the_leveling_task_group():
    """代跑别的任务时读侵蚀1 那份，而不是读不到就当关闭。"""
    config = _Config(task='OpsiScheduling', research_enable=False)
    stub = GateStub(config)
    assert stub._is_siren_research_enabled is False
    assert any('OpsiHazard1Leveling.OpsiSirenBug' in keys for keys in config.queried)


def test_skip_only_a_device_grid_with_the_switch_off():
    off = GateStub(_Config(research_enable=False))
    assert off._should_skip_siren_research(DeviceGrid()) is True
    # 普通格子（问号开出来的剧情、明石、记录塔）不受这个开关影响
    assert off._should_skip_siren_research(PlainGrid()) is False

    on = GateStub(_Config(research_enable=True))
    assert on._should_skip_siren_research(DeviceGrid()) is False

    missing = GateStub(_Config(has_key=False))
    assert missing._should_skip_siren_research(DeviceGrid()) is False


# ---------------------------------------------------------------- map_rescan_current

class DeviceView:
    def __init__(self, grid):
        self._grid = grid
        self.device_seen = 0

    def select(self, **kwargs):
        if kwargs.get('is_scanning_device'):
            self.device_seen += 1
            return [self._grid]
        return []


class RescanStub:
    """跑真实的 `map_rescan_current`，只关心装置分支的闸门有没有早退。"""

    map_rescan_current = OSMap.map_rescan_current
    _mark_event_unreachable = OSMap._mark_event_unreachable
    _siren_device_search_plan = OSMap._siren_device_search_plan
    _should_skip_siren_research = OSMap._should_skip_siren_research
    _is_siren_research_enabled = OSMap._is_siren_research_enabled

    is_in_task_explore = False

    def __init__(self, research_enable=True, has_key=True, grid=None, mode='resource'):
        self.view = DeviceView(grid if grid is not None else DeviceGrid())
        self._solved_map_event = set()
        self._unreachable_event_nodes = set()
        self.clicks = 0
        self.auto_searches = 0
        self.states = []
        self.fleet_sets = []
        self.siren_device_mode = mode
        self.is_siren_device_confirmed = False
        self.config = _Config(research_enable=research_enable, has_key=has_key)
        self.device = SimpleNamespace(image=None, click=self._click, screenshot=lambda: None)
        self.fleet_selector = SimpleNamespace(get=lambda: 1)

    def _click(self, grid):
        self.clicks += 1

    def _set_device_state(self, state):
        self.states.append(state)

    def _goto_scanning_device_with_other_fleets(self, drop=None):
        return False

    def wait_until_walk_stable(self, **kwargs):
        # 真实现里剧情选项被认成装置剧情才置位（story_skip -> _identify_siren_device_option）
        self.is_siren_device_confirmed = True
        return 'event'

    def os_auto_search_run(self, drop=None):
        self.auto_searches += 1

    def fleet_set(self, fleet):
        self.fleet_sets.append(fleet)

    def update(self):
        pass


def test_rescan_switched_off_is_not_touched_at_all():
    """关闭时：不点击、不行军、不开对话、不跑自律、不碰装置状态，直接标记已解决。"""
    stub = RescanStub(research_enable=False)
    assert stub.map_rescan_current() is True
    assert stub.clicks == 0
    assert stub.auto_searches == 0
    assert stub.states == []
    assert stub.view.device_seen == 1          # 只识别了一遍，没有二次补扫
    assert stub._solved_map_event == {'is_scanning_device'}


def test_rescan_switched_off_with_key_missing_still_handles_the_device():
    """老配置读不到开关 -> 按默认值 True 处理，闸门不许误拦。"""
    stub = RescanStub(has_key=False)
    assert stub.map_rescan_current() is True
    assert stub.clicks == 1
    assert stub.auto_searches == 1


def test_rescan_switched_on_keeps_the_original_flow():
    """回归保护：开着的时候行为必须一点不变。"""
    stub = RescanStub(research_enable=True)
    assert stub.map_rescan_current() is True
    assert stub.clicks == 1
    assert stub.auto_searches == 1
    assert stub.states == [DEVICE_TARGETED, DEVICE_DIALOG_OPEN, DEVICE_COMPLETED]
    assert stub._solved_map_event == {'is_scanning_device'}


# ---------------------------------------------------------------- clear_question

class RadarStub:
    def predict_question(self, image, in_port=False):
        return (0, -1)


class ClearView:
    def __init__(self, grid):
        self._grid = grid
        self.predicted = 0

    def predict(self):
        self.predicted += 1

    def show(self):
        pass

    def select(self, **kwargs):
        if kwargs.get('is_current_fleet'):
            return FleetSeen()
        return []


class ClearStub:
    """跑真实的 `clear_question`；雷达上永远有一个问号。"""

    clear_question = OSMap.clear_question
    _should_skip_siren_research = OSMap._should_skip_siren_research
    _is_siren_research_enabled = OSMap._is_siren_research_enabled
    _os_camera_recover_to_fleet = OSMap._os_camera_recover_to_fleet

    def __init__(self, research_enable=True, has_key=True, grid=None, walk_result='akashi'):
        self._grid = grid if grid is not None else DeviceGrid()
        self.radar = RadarStub()
        self.view = ClearView(self._grid)
        self._solved_map_event = set()
        self._question_unreachable = False
        self.is_siren_device_confirmed = False
        self.siren_device_mode = None
        self.clicks = 0
        self.waits = 0
        self.auto_searches = 0
        self.zone = SimpleNamespace(is_port=False)
        self.config = _Config(research_enable=research_enable, has_key=has_key)
        self.device = SimpleNamespace(image=None, click=self._click)
        self.fleet_selector = SimpleNamespace(get=lambda: 1)
        self._walk_result = walk_result

    def _click(self, grid):
        self.clicks += 1

    def handle_info_bar(self):
        pass

    def update_os(self):
        pass

    def convert_radar_to_local(self, location):
        return self._grid

    def wait_until_walk_stable(self, **kwargs):
        self.waits += 1
        return self._walk_result

    def os_auto_search_run(self, drop=None):
        self.auto_searches += 1

    def fleet_set(self, fleet):
        pass


def test_clear_question_switched_off_never_walks_over():
    """关闭时：雷达看到装置格上的问号也不点、不走、不跑自律，直接标记已解决。"""
    stub = ClearStub(research_enable=False)
    assert stub.clear_question() is True
    assert stub.clicks == 0
    assert stub.waits == 0
    assert stub.auto_searches == 0
    assert stub._solved_map_event == {'is_scanning_device'}


def test_clear_question_switched_on_still_clicks():
    """回归保护：开着的时候照旧点击并处理（'akashi' -> is_akashi）。"""
    stub = ClearStub(research_enable=True)
    assert stub.clear_question() is True
    assert stub.clicks == 1
    assert stub.waits == 1
    assert stub._solved_map_event == {'is_akashi'}


def test_clear_question_plain_grid_is_not_gated():
    """问号开出的是普通剧情事件时不走闸门：照旧点满 3 次尝试，只是没有装置标记。"""
    stub = ClearStub(research_enable=False, grid=PlainGrid(), walk_result='event')
    assert stub.clear_question() is False
    assert stub.clicks == 3
    assert stub.waits == 3
    assert stub.auto_searches == 0
    assert stub._solved_map_event == set()
    assert stub._question_unreachable is True


def test_clear_question_switched_off_never_runs_the_extra_auto_search():
    """这条差异的直接症状：关闭时不该出现"装置已处理 + 多跑一轮自律寻敌"。"""
    stub = ClearStub(research_enable=False, walk_result='event')
    assert stub.clear_question() is True
    assert stub.auto_searches == 0
    assert stub.waits == 0
