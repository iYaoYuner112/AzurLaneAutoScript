"""补票：AP 早就改掉、我们还在跑旧行为的四处大世界逻辑。

1. `nearest_task_cooling_down`：已过期的任务不能算"冷却中"（AP d43a1361e「修复大世界可能在
   某个任务死循环的问题」）。旧判据只有上界，把过去的 next_run 传给 `task_delay(target=…)`
   会让等待任务同轮立即重跑，而且返回值非 None 时隐秘/深渊/要塞根本不被唤起。
2. `os_map_goto_globe`：到了全球地图、但**没有可取消置顶的海域**时也要收工（AP edb9c11f1
   「修复全球地图等待卡死」）。旧写法要求至少取消成功过一次，否则 break 永不触发，只能撑到
   设备卡死检测重启游戏。
3. `OpsiDaily`：进不去的委托海域跳过并继续别的（AP 5979fc1cb）。旧写法只兜 `ActionPointLimit`，
   一个被锁住的海域能把整个每日任务打死；现在加了连续上限，避免同一格被无限重挑。
4. 短猫随机海域：没有可用海域时优雅收工（AP 30b5a2b7d），不再 `zones[0]` 报 IndexError。
"""

from datetime import datetime, timedelta
from time import sleep
from types import SimpleNamespace

from module.os.globe_operation import GlobeOperation, OSExploreError
from module.os.tasks.daily import OpsiDaily
from module.os.tasks.meowfficer_farming import OpsiMeowfficerFarming
from module.os_handler.mission import MissionHandler
from module.os_handler.os_status import OSStatus

NOW = datetime(2026, 10, 5, 12, 0, 0)
UPDATE = NOW + timedelta(hours=6)


def cooling(command='OpsiDaily', enable=True, next_run=None):
    task = SimpleNamespace(command=command, enable=enable,
                           next_run=next_run if next_run is not None else NOW + timedelta(minutes=10))
    return OSStatus.task_cooling_soon(task, NOW, UPDATE)


def test_due_task_is_not_a_cooldown():
    """这条就是 AP 修的那个死循环：已到期的任务不算冷却。"""
    assert cooling(next_run=NOW - timedelta(minutes=10)) is False
    assert cooling(next_run=NOW) is False


def test_task_ready_within_an_hour_is_a_cooldown():
    assert cooling(next_run=NOW + timedelta(minutes=10)) is True
    assert cooling(next_run=NOW + timedelta(minutes=59)) is True


def test_task_beyond_the_window_is_not_a_cooldown():
    assert cooling(next_run=NOW + timedelta(minutes=90)) is False


def test_daily_reset_task_is_not_a_cooldown():
    """排在日更点上的任务交给调度处理，不当冷却。"""
    assert cooling(next_run=UPDATE) is False


def test_only_the_four_opsi_tasks_can_block():
    assert cooling(command='OpsiExplore') is False
    assert cooling(command='OpsiMeowfficerFarming') is False
    for command in ('OpsiObscure', 'OpsiAbyssal', 'OpsiStronghold', 'OpsiDaily'):
        assert cooling(command=command) is True, command


def test_disabled_task_is_not_a_cooldown():
    assert cooling(enable=False) is False


class GlobeStub:
    """`os_map_goto_globe` 的两次循环：第一循环立刻认定已到全球地图。"""

    os_map_goto_globe = GlobeOperation.os_map_goto_globe

    def __init__(self, pinned_frames=0):
        self.pinned_frames = pinned_frames
        self.frames = 0
        self.loops = 0

    def loop(self, *args, **kwargs):
        self.loops += 1
        for _ in range(60):
            self.frames += 1
            # 生产代码里每帧之间隔着一张截图（约 0.5 秒），退出条件用的是 1 秒 + 次数双门槛，
            # 所以测试必须让时间真的往前走，否则会误判成"永远不退出"
            sleep(0.05)
            yield

    def is_in_globe(self):
        return True

    def is_zone_pinned(self):
        return False

    def handle_zone_pinned(self):
        return self.frames <= self.pinned_frames


def test_globe_return_breaks_without_any_unpin():
    """一个置顶都没有时也必须收工：旧写法会一路滑到设备卡死。"""
    stub = GlobeStub()
    stub.os_map_goto_globe()
    assert stub.loops == 2, stub.loops
    assert stub.frames < 30, stub.frames


def test_globe_return_still_finishes_after_unpinning():
    stub = GlobeStub(pinned_frames=3)
    stub.os_map_goto_globe()
    assert stub.loops == 2, stub.loops
    assert stub.frames < 30, stub.frames


def test_globe_return_without_unpin_keeps_old_exit():
    """unpin=False 那条分支没动过：看见置顶海域就收工（离开海域后 normally 就有）。"""
    stub = GlobeStub()
    stub.is_zone_pinned = lambda: True
    stub.os_map_goto_globe(unpin=False)
    assert stub.loops == 2, stub.loops
    assert stub.frames <= 3, stub.frames


class DailyStub:
    """`os_finish_daily_mission`：脚本控制每次取委托是报错、成功还是没委托了。"""

    os_finish_daily_mission = OpsiDaily.os_finish_daily_mission
    _is_daily_mission_task = OpsiDaily._is_daily_mission_task
    _os_return_from_unavailable_mission = MissionHandler._os_return_from_unavailable_mission
    OS_DAILY_UNAVAILABLE_ZONE_LIMIT = MissionHandler.OS_DAILY_UNAVAILABLE_ZONE_LIMIT

    def __init__(self, script, daily_task=True):
        self.script = list(script)
        self.daily_task = daily_task
        self.picks = 0
        self.returns = 0
        self.searches = 0
        self.zone = SimpleNamespace(is_port=False, zone_id=44)
        self.config = SimpleNamespace(
            task=SimpleNamespace(command='OpsiDaily' if daily_task else 'OpsiStronghold'),
            _opsi_context=SimpleNamespace(current_task='OpsiDaily' if daily_task else 'OpsiStronghold'),
            OpsiFleet_Fleet=1,
            OpsiFleet_Submarine=False,
            check_task_switch=lambda: None,
        )

    def os_get_next_mission(self, skip_siren_mission=False):
        self.picks += 1
        item = self.script[min(self.picks - 1, len(self.script) - 1)]
        if item == 'error':
            raise OSExploreError
        return item

    def zone_init(self):
        pass

    def globe_goto(self, zone, types=None, refresh=False):
        pass

    def fleet_set(self, index=1):
        pass

    def os_order_execute(self, recon_scan=True, submarine_call=True):
        pass

    def run_auto_search(self, question=True, rescan=None, interrupt=None):
        self.searches += 1

    def handle_after_auto_search(self):
        pass

    def ensure_no_zone_pinned(self):
        pass

    def os_globe_goto_map(self, skip_first_screenshot=True):
        self.returns += 1


def test_unenterable_mission_zone_is_skipped_and_the_flow_continues():
    stub = DailyStub(['error', 'pinned_at_mission_zone', False])
    assert stub.os_finish_daily_mission() == 1
    assert stub.searches == 1, stub.searches
    assert stub.returns == 1, stub.returns


def test_same_bad_zone_cannot_loop_forever():
    """海域仍在委托列表里，必须靠连续上限收工，不能无限重挑。"""
    stub = DailyStub(['error'] * 10)
    assert stub.os_finish_daily_mission() == 0
    assert stub.picks == MissionHandler.OS_DAILY_UNAVAILABLE_ZONE_LIMIT, stub.picks
    assert stub.returns == MissionHandler.OS_DAILY_UNAVAILABLE_ZONE_LIMIT, stub.returns


def test_counter_resets_after_a_good_zone():
    stub = DailyStub(['error', 'pinned_at_mission_zone', 'error', 'error', False])
    assert stub.os_finish_daily_mission() == 1
    assert stub.picks == 5, stub.picks


def test_other_tasks_still_see_the_error():
    """不是每日任务本体时（档案、月末）保持原样上抛，交给它们自己的处理。"""
    stub = DailyStub(['error'], daily_task=False)
    raised = False
    try:
        stub.os_finish_daily_mission()
    except OSExploreError:
        raised = True
    assert raised
    assert stub.returns == 0


def test_daily_identity_recognises_the_proxy():
    """调度代跑每日时也算每日本体，否则这条兜底永远不生效。"""
    def holder(command, current):
        return SimpleNamespace(config=SimpleNamespace(
            task=SimpleNamespace(command=command),
            _opsi_context=None if current is None else SimpleNamespace(current_task=current),
        ))

    assert OpsiDaily._is_daily_mission_task(holder('OpsiScheduling', 'OpsiDaily')) is True
    assert OpsiDaily._is_daily_mission_task(holder('OpsiDaily', None)) is True
    assert OpsiDaily._is_daily_mission_task(holder('OpsiScheduling', 'OpsiStronghold')) is False
    assert OpsiDaily._is_daily_mission_task(holder('OpsiScheduling', None)) is False


class ZoneList(list):
    def delete(self, grids):
        return self

    def select(self, **kwargs):
        return ZoneList([])

    def sort_by_clock_degree(self, center=None, start=None):
        return self


class MeowStub:
    """`_meow_handle_normal_search`：随机海域为空时不许再碰 zones[0]。"""

    _meow_handle_normal_search = OpsiMeowfficerFarming._meow_handle_normal_search

    def __init__(self, zones=()):
        self._zones = ZoneList(zones)
        self.zone = SimpleNamespace(is_port=False, zone_id=44, location=(0, 0))
        self.zones = ZoneList([])
        self.delay = []
        self.stopped = False
        self.entered = None
        self.searched = 0
        self.config = SimpleNamespace(
            OpsiMeowfficerFarming_HazardLevel=4,
            OpsiFleet_Fleet=1,
            OpsiFleet_Submarine=False,
            task_delay=lambda server_update=False, target=None: self.delay.append(server_update),
            task_stop=lambda: self._stop(),
            check_task_switch=lambda: None,
        )

    def _stop(self):
        self.stopped = True

    def zone_select(self, hazard_level=None):
        return self._zones

    def globe_goto(self, zone, types=None, refresh=False):
        self.entered = zone

    def fleet_set(self, index=1):
        pass

    def os_order_execute(self, recon_scan=True, submarine_call=True):
        pass

    def run_auto_search(self, question=True, rescan=None, interrupt=None):
        self.searched += 1

    def _meow_fixed_patrol_scan(self):
        pass

    def handle_after_auto_search(self):
        pass


def zone(zone_id):
    return SimpleNamespace(zone_id=zone_id, is_port=False, location=(0, 0))


def test_empty_zone_list_ends_the_round_nicely():
    stub = MeowStub()
    assert stub._meow_handle_normal_search() is False
    assert stub.entered is None
    assert stub.searched == 0
    # 没有海域可做就延到日更，别立刻空转再来一轮
    assert stub.delay == [True]
    assert stub.stopped is True


def test_normal_search_still_runs_with_zones_left():
    stub = MeowStub([zone(44), zone(45)])
    stub._meow_handle_normal_search()
    assert stub.entered is not None
    assert stub.searched == 1
    assert stub.delay == []
