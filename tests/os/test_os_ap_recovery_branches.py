"""AP 的四处「卡住/点反」兜底，对齐后锁行为。

对应的 AP 改动：
1. `AutoSearchSwitch`：`AUTO_SEARCH_ON` 与 `AUTO_SEARCH_OFF` **共享同一点击区域**，
   所以「当前状态读不出来」时绝不能点——点下去可能把已经开着的关掉。
2. `os_globe_goto_map`：从全球地图进海域图时可能误点进港口，要自己退出来重试。
3. `port_shop_quit`：退港口商店时可能误进情报总览（要用 order_quit 退），
   退出情报后可能落在大地图（要重进港口）。
4. `storage_enter`：进仓库时可能误进情报界面或全球地图，都要先退出去。
5. `handle_map_event`：余烬信标弹窗也必须排在通用确认之前（先匹配它会被误点确认、进 META 页卡死）。
"""

import time
from types import SimpleNamespace

import module.handler.fast_forward as fast_forward
from module.handler.fast_forward import FastForwardHandler
from module.os.globe_operation import GlobeOperation
from module.os_handler.map_event import MapEventHandler
from module.os_handler.port import PORT_CHECK, PortHandler
from module.os_handler.storage import StorageHandler
from module.os_handler.assets import MISSION_CHECK, MISSION_QUIT, ORDER_CHECK, STORAGE_ENTER
from module.os_shop.assets import PORT_SUPPLY_CHECK
from module.ui.assets import BACK_ARROW


# ---------------------------------------------------------------- 通用替身

class NameStub:
    """按 `button.name` 判定界面的替身，记录点击与调用过的方法名。

    `frames` = 已经截了多少张图，`_frame()` 取的就是当前这一帧的画面。
    """

    def __init__(self):
        self.seen = set()
        self.clicks = []
        self.calls = []
        self.frames = 0

    def screenshot(self):
        self.frames += 1

    def appear(self, button, offset=0, interval=0, **kwargs):
        return getattr(button, 'name', '') in self.seen

    def click(self, button, **kwargs):
        self.clicks.append(getattr(button, 'name', str(button)))
        self.frames += 1

    @property
    def device(self):
        return self


# ---------------------------------------------------------------- 1. 自动搜索开关

class FakeAutoSearch:
    def __init__(self, states):
        self.states = list(states)
        self.reads = 0
        self.clicks = 0

    def get(self, main=None):
        # 慢一点，免得 timeout 那轮空转烧 CPU
        time.sleep(0.05)
        i = min(self.reads, len(self.states) - 1)
        self.reads += 1
        return self.states[i]

    def click(self, state, main=None):
        self.clicks += 1


class AutoSearchStub:
    _auto_search_set = FastForwardHandler._auto_search_set
    handle_auto_search = FastForwardHandler.handle_auto_search

    def __init__(self, use_auto_search=False, map_is_auto_search=False):
        self.map_is_auto_search = map_is_auto_search
        self.config = SimpleNamespace(Campaign_UseAutoSearch=use_auto_search)
        self.device = SimpleNamespace(screenshot=lambda: None)


def with_auto_search(switch, func):
    original = fast_forward.AUTO_SEARCH
    fast_forward.AUTO_SEARCH = switch
    try:
        return func()
    finally:
        fast_forward.AUTO_SEARCH = original


def test_unknown_state_is_never_clicked():
    """当前状态读不出来时只能等，绝不能点：点 ON 区域会把已开着的关掉。"""
    switch = FakeAutoSearch(['unknown'])
    stub = AutoSearchStub()
    changed = with_auto_search(switch, lambda: stub._auto_search_set('on', current='unknown'))
    assert changed is False
    assert switch.clicks == 0, switch.clicks


def test_off_is_turned_on_once():
    switch = FakeAutoSearch(['off', 'on'])
    stub = AutoSearchStub()
    changed = with_auto_search(switch, lambda: stub._auto_search_set('on', current='off'))
    assert changed is True
    assert switch.clicks == 1, switch.clicks


def test_state_already_reached_is_not_clicked():
    switch = FakeAutoSearch(['on'])
    stub = AutoSearchStub()
    changed = with_auto_search(switch, lambda: stub._auto_search_set('on', current='on'))
    assert changed is False
    assert switch.clicks == 0


def test_no_option_keeps_auto_search_off_and_clicks_nothing():
    """地图上没有自动搜索选项时：不许点，而且要把标志清掉，否则关卡任务会按自律寻敌跑。"""
    switch = FakeAutoSearch(['unknown'])
    stub = AutoSearchStub(use_auto_search=True, map_is_auto_search=True)
    changed = with_auto_search(switch, stub.handle_auto_search)
    assert changed is False
    assert switch.clicks == 0
    assert stub.map_is_auto_search is False


def test_config_enabled_forces_the_switch_on():
    """配置开了自律寻敌但清关模式没确认时，按 AP 保持开启并真的去把它点上。"""
    switch = FakeAutoSearch(['off', 'on'])
    stub = AutoSearchStub(use_auto_search=True, map_is_auto_search=False)
    changed = with_auto_search(switch, stub.handle_auto_search)
    assert changed is True
    assert switch.clicks == 1
    assert stub.map_is_auto_search is True


# ---------------------------------------------------------------- 2. 全球地图回海域图

class GlobeStub(NameStub):
    os_globe_goto_map = GlobeOperation.os_globe_goto_map

    def __init__(self, script):
        super().__init__()
        self.script = list(script)

    def loop(self, *args, **kwargs):
        # 这个函数每一轮自己截图，所以帧数靠 `screenshot()` 推进
        for _ in range(30):
            yield

    def _frame(self):
        return self.script[min(self.frames, len(self.script) - 1)]

    def is_in_map(self):
        self.seen = set(self._frame())
        return 'IN_MAP' in self.seen

    def appear_then_click(self, button, offset=0, interval=0, **kwargs):
        if getattr(button, 'name', '') in self.seen:
            self.clicks.append(button.name)
            return True
        return False

    def interval_reset(self, button):
        pass


def test_globe_to_map_stops_on_the_map():
    stub = GlobeStub([{'IN_MAP'}])
    assert stub.os_globe_goto_map() is True
    assert stub.clicks == []


def test_globe_to_map_backs_out_of_an_accidental_port():
    """AP：误点进港口要自己退出来，不然永远找不到 GLOBE_GOTO_MAP。"""
    stub = GlobeStub([{PORT_CHECK.name}, {'IN_MAP'}])
    assert stub.os_globe_goto_map() is True
    assert stub.clicks == [BACK_ARROW.name], stub.clicks


# ---------------------------------------------------------------- 3. 退港口商店

class PortStub(NameStub):
    port_shop_quit = PortHandler.port_shop_quit

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.keys = []

    def _frame(self):
        return self.script[min(self.frames, len(self.script) - 1)]

    def appear(self, button, offset=0, interval=0, **kwargs):
        return button.name in self._frame()

    def is_in_map(self):
        return 'IN_MAP' in self._frame()

    def order_quit(self):
        self.keys.append('order_quit')

    def port_enter(self):
        self.keys.append('port_enter')

    def interval_clear(self, buttons):
        pass

    def interval_reset(self, button):
        pass

    def ui_back(self, **kwargs):
        self.keys.append('ui_back')


def test_port_shop_quit_stops_at_the_port():
    stub = PortStub([{'PORT_GOTO_SUPPLY'}])
    stub.port_shop_quit()
    assert stub.keys == []
    assert stub.clicks == []


def test_port_shop_quit_quits_the_order_overview():
    """误进情报总览时要用 order_quit 退，不能一直点返回箭头。"""
    order = {ORDER_CHECK.name}
    port = {'PORT_GOTO_SUPPLY'}
    stub = PortStub([order, port])
    stub.port_shop_quit()
    assert stub.keys == ['order_quit'], stub.keys


def test_port_shop_quit_reenters_the_port_after_the_order_overview():
    """退出情报总览后可能落在大地图，此时要重新进港口。"""
    order = {ORDER_CHECK.name}
    on_map = {'IN_MAP'}
    port = {'PORT_GOTO_SUPPLY'}
    stub = PortStub([order, on_map, port])
    stub.port_shop_quit()
    assert stub.keys == ['order_quit', 'port_enter'], stub.keys


def test_port_shop_quit_falls_back_to_the_back_button():
    """一直在商店页时走原来的返回箭头路径。"""
    supply = {PORT_SUPPLY_CHECK.name}
    port = {'PORT_GOTO_SUPPLY'}
    stub = PortStub([supply, port])
    stub.port_shop_quit()
    assert stub.clicks == [BACK_ARROW.name], stub.clicks


# ---------------------------------------------------------------- 4. 进仓库

class StorageStub(NameStub):
    storage_enter = StorageHandler.storage_enter

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        # 这个函数不自己截图，帧靠 `loop()` 推进
        self.iter = 0

    def loop(self, *args, **kwargs):
        for _ in range(20):
            self.iter += 1
            yield

    def _frame(self):
        return self.script[min(self.iter - 1, len(self.script) - 1)]

    def is_in_storage(self):
        return 'STORAGE' in self._frame()

    def is_in_map(self):
        return 'IN_MAP' in self._frame()

    def is_in_globe(self):
        return 'GLOBE' in self._frame()

    def appear(self, button, offset=0, interval=0, **kwargs):
        return button.name in self._frame()

    def appear_then_click(self, button, offset=0, interval=0, **kwargs):
        if button.name in self._frame():
            self.clicks.append(button.name)
            return True
        return False

    def ui_click(self, button, **kwargs):
        self.calls.append('ui_click:' + button.name)

    def os_globe_goto_map(self):
        self.calls.append('os_globe_goto_map')

    def handle_map_event(self):
        self.calls.append('handle_map_event')
        return False

    def handle_info_bar(self):
        pass


def test_storage_enter_stops_at_the_storage():
    stub = StorageStub([{'STORAGE'}])
    stub.storage_enter()
    assert stub.calls == []


def test_storage_enter_quits_the_mission_list():
    mission = {MISSION_CHECK.name}
    stub = StorageStub([mission, {'STORAGE'}])
    stub.storage_enter()
    assert stub.calls == ['ui_click:' + MISSION_QUIT.name], stub.calls


def test_storage_enter_goes_back_from_the_globe():
    stub = StorageStub([{'GLOBE'}, {'STORAGE'}])
    stub.storage_enter()
    assert stub.calls == ['os_globe_goto_map'], stub.calls


# ---------------------------------------------------------------- 5. 地图事件顺序

class EventStub:
    handle_map_event = MapEventHandler.handle_map_event

    def __init__(self, ash=False, confirm=False, get_items=False):
        self.ash = ash
        self.confirm = confirm
        self.get_items = get_items
        self.calls = []

    def handle_ash_popup(self):
        self.calls.append('ash')
        return self.ash

    def handle_popup_confirm(self, name='', **kwargs):
        self.calls.append('confirm:' + name)
        return self.confirm

    def handle_map_get_items(self, drop=None):
        self.calls.append('get_items')
        return self.get_items

    def handle_os_game_tips(self):
        self.calls.append('game_tips')
        return False

    def handle_map_archives(self, drop=None):
        self.calls.append('archives')
        return False

    def handle_guild_popup_cancel(self):
        self.calls.append('guild')
        return False

    def handle_urgent_commission(self, drop=None):
        self.calls.append('urgent')
        return False

    def handle_story_skip(self):
        self.calls.append('story')
        return False


def test_ash_popup_is_matched_before_the_generic_confirm():
    """余烬弹窗也有确认/取消，先匹配通用确认会误点确认直接进 META 页。"""
    stub = EventStub(ash=True, confirm=True)
    assert stub.handle_map_event() == 'ash_popup'
    assert stub.calls == ['ash'], stub.calls


def test_depart_confirm_is_handled():
    """指挥猫搜寻时退出海域的确认弹窗：必须主动处理，不然卡住自律寻敌。"""
    stub = EventStub(ash=False, confirm=True)
    assert stub.handle_map_event() == 'depart_confirm'
    assert 'confirm:DEPART_CONFIRM' in stub.calls


def test_depart_confirm_comes_before_the_other_popups():
    stub = EventStub(ash=False, confirm=True, get_items=True)
    assert stub.handle_map_event() == 'depart_confirm'
    assert stub.calls == ['ash', 'confirm:DEPART_CONFIRM'], stub.calls


def test_nothing_appeared_returns_empty():
    stub = EventStub()
    assert stub.handle_map_event() == ''
