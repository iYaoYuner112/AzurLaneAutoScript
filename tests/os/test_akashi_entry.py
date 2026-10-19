"""明石入口：点击夹进安全区、误入页面恢复、失败不算成功、重启续查。

对应 AzurPilot dev `e8a948f01`「修复明石与按钮重合时的点击及重启漏猫」的行为目标
（AP 抽了独立模块 `module/os/akashi.py`，我们按行为移植进现有结构）：

1. 明石常站在地图边缘，和固定 UI（左下舰队条 / 底部按钮排 / 右侧按钮列）重合。点击落到那些按钮上，
   开的是船坞、舰队面板甚至白票商店，**商店根本没开** —— 事件却被当成"处理过了"。
   → 所以点击框必须夹进地图安全区 `(239, 128, 993, 628)`。
2. 入口循环有预算：12 秒无进展、最多点 3 次、误入页面恢复最多 4 次、镜头最多挪 2 次。
3. 万一点进了别的页面，先退回来再点。
4. **只有「进店 + 买完 + 回到地图」才算成功**。失败不能记成明石已处理，否则这个事件就永久丢了。
5. 侵蚀1确认明石后立即持久化，成功处理前跨重启保留（重启续查）。
"""

from types import SimpleNamespace
from unittest.mock import Mock

from module.base.button import Button
from module.base.timer import Timer
from module.config.deep import deep_get, deep_set
from module.config.utils import get_os_next_reset
from module.os.akashi import AkashiRecovery, PENDING_AKASHI_PATH
from module.os.map import OSMap
from module.os_shop.assets import PORT_SUPPLY_CHECK
from module.os_shop.shop import OSShop


class Grid:
    """只提供 `button` / `location` / `__str__`，够夹取逻辑用。"""

    def __init__(self, button, location=(3, 3)):
        self.button = button
        self.location = location

    def __str__(self):
        return 'C3'


# ---------------------------------------------------------------- 夹取逻辑

def test_partially_covered_grid_is_clamped_into_the_map():
    """下沿被底部按钮排压住：夹掉被压的那截，剩下的仍然可点。"""
    grid = Grid(button=(300, 600, 380, 700))
    button = OSShop._akashi_safe_click_button(grid)
    assert isinstance(button, Button)
    assert tuple(button.button) == (300, 600, 380, 628), button.button


def test_grid_under_the_bottom_bar_is_rejected():
    """整块落在底部按钮排里，夹完什么都不剩 → 不允许点。"""
    assert OSShop._akashi_safe_click_button(Grid(button=(300, 640, 380, 700))) is None


def test_grid_under_the_fleet_bar_is_rejected():
    """整块落在左下舰队条里 → 不允许点。"""
    assert OSShop._akashi_safe_click_button(Grid(button=(150, 500, 220, 560))) is None


def test_grid_without_button_area_is_rejected():
    """没有可点区域的格子不能崩，直接判不可点。"""
    assert OSShop._akashi_safe_click_button(object()) is None


# ---------------------------------------------------------------- 入口循环

class FleetSelectorStub:
    def __init__(self, opened):
        self.opened = opened

    def bar_opened(self):
        return self.opened


class ShopStub:
    """挂真实现，把外部交互（识别、点击、界面切换）换成可控制的假动作。

    `open_after_clicks`：第 N 次点击后商店才打开；None 表示永远打不开。
    """

    _akashi_safe_click_button = staticmethod(OSShop._akashi_safe_click_button)
    _recover_akashi_page = OSShop._recover_akashi_page
    handle_akashi_supply_buy = OSShop.handle_akashi_supply_buy

    def __init__(self, grid, *, page='map', open_after_clicks=None, back_to_map=True,
                 menu=False, recoveries=0):
        self.grid = grid
        self.page = page
        self.open_after_clicks = open_after_clicks
        self.back_to_map = back_to_map
        self.recoveries_left = recoveries
        self.clicks = []
        self.bought = 0
        self.finished = 0
        self.remembered = []
        self.recovered = 0
        self.fleet_selector = FleetSelectorStub(menu)
        self.device = SimpleNamespace(click=self._click)
        self.handle_story_skip = Mock(return_value=False)
        self.os_shop_buy = Mock(side_effect=lambda select_func: setattr(
            self, 'bought', self.bought + 1))
        self.ui_back = Mock(side_effect=lambda **kwargs: setattr(
            self, 'page', 'map' if self.back_to_map else self.page))

    def _click(self, button):
        self.clicks.append(button.name)
        if self.open_after_clicks is not None and len(self.clicks) >= self.open_after_clicks:
            self.page = 'shop'

    def _remember_pending_akashi(self, grid=None):
        self.remembered.append(grid)

    def _finish_pending_akashi(self):
        self.finished += 1

    def _akashi_global_location(self, grid):
        return None

    def _recover_akashi_page(self):
        if self.recoveries_left > 0:
            self.recoveries_left -= 1
            self.recovered += 1
            return True
        return False

    def appear(self, button, offset=(20, 20), **kwargs):
        return button is PORT_SUPPLY_CHECK and self.page == 'shop'

    def is_in_map(self):
        return self.page == 'map'

    def os_shop_buy(self, select_func):
        self.bought += 1

    def os_shop_get_item_to_buy_in_akashi(self):
        return None

    def ui_back(self, **kwargs):
        pass

    def loop(self, skip_first=True, timeout=None):
        """有限次迭代防止测试死循环；真实实现是无界循环，靠预算退出。"""
        for i in range(60):
            yield
        raise AssertionError('entry loop did not terminate in test')


SAFE_BUTTON = Button(area=(300, 300, 360, 360), color=(), button=(300, 300, 360, 360),
                     name='AKASHI_C3')


def test_success_needs_entry_purchase_and_map():
    stub = ShopStub(Grid(button=(300, 300, 360, 360)), open_after_clicks=1)
    assert stub.handle_akashi_supply_buy(stub.grid) is True
    assert stub.clicks == ['AKASHI_C3']
    assert stub.bought == 1
    assert stub.finished == 1
    assert len(stub.remembered) == 1


def test_shop_already_open_skips_the_click():
    """行走过程已经把商店打开了（fleet 的循环接住的那种）：不再点，只买单。"""
    stub = ShopStub(Grid(button=(300, 300, 360, 360)), page='shop')
    assert stub.handle_akashi_supply_buy(stub.grid) is True
    assert stub.clicks == []
    assert stub.bought == 1


def test_covered_grid_is_not_counted_as_done():
    """点不到就返回 False，而且根本不该去走购买流程。"""
    stub = ShopStub(Grid(button=(300, 640, 380, 700)))
    assert stub.handle_akashi_supply_buy(stub.grid) is False
    assert stub.bought == 0


def test_click_budget_is_three():
    """第 3 次点击后就不再点第 4 下（对齐 AzurPilot 的 clicks < 3）；
    12 秒无进展预算烧完后入口判负。用虚拟时钟快进，不真等。"""
    from unittest.mock import patch

    class Clock:
        now = 1000.0

        def __call__(self):
            self.now += 0.5  # 每次读时钟走半秒，让 12 秒预算在受控循环里烧完
            return self.now

        def advance(self, seconds):
            self.now += seconds

    clock = Clock()
    stub = ShopStub(Grid(button=(300, 300, 360, 360)), open_after_clicks=4)
    with patch('module.base.timer.time', clock):
        assert stub.handle_akashi_supply_buy(stub.grid) is False
    assert len(stub.clicks) == 3


def test_not_returning_to_map_is_not_counted_as_done():
    stub = ShopStub(Grid(button=(300, 300, 360, 360)), open_after_clicks=1,
                    back_to_map=False)
    assert stub.handle_akashi_supply_buy(stub.grid) is False
    assert stub.bought == 1
    assert stub.finished == 0


# ---------------------------------------------------------------- 误入页面恢复

class PageStub:
    _recover_akashi_page = OSShop._recover_akashi_page

    def __init__(self, page, bar_opened=False):
        self.page = page
        self.left = []
        self.fleet_selector = FleetSelectorStub(bar_opened)

    def is_in_globe(self):
        return self.page == 'globe'

    def is_in_storage(self):
        return self.page == 'storage'

    def is_in_os_mission(self):
        return self.page == 'mission'

    def is_in_map_order(self):
        return self.page == 'order'

    def is_in_map(self):
        return self.page == 'map'

    def os_globe_goto_map(self):
        self.left.append('globe')

    def storage_quit(self):
        self.left.append('storage')

    def os_mission_quit(self):
        self.left.append('mission')

    def order_quit(self):
        self.left.append('order')

    def appear_then_click(self, button, **kwargs):
        self.left.append('fleet_bar')
        return True


def test_each_wrong_page_is_left():
    for page in ('globe', 'storage', 'mission', 'order'):
        stub = PageStub(page)
        assert stub._recover_akashi_page() is True, page
        assert stub.left == [page], (page, stub.left)


def test_map_page_needs_no_recovery():
    stub = PageStub('map')
    assert stub._recover_akashi_page() is False
    assert stub.left == []


def test_open_menu_is_closed_before_any_map_click():
    stub = PageStub('map', bar_opened=True)
    assert stub._recover_akashi_page() is True
    assert stub.left == ['fleet_bar']


# ---------------------------------------------------------------- 重启续查

class ConfigStub:
    def __init__(self, command='OpsiHazard1Leveling', fleet=1, server='cn'):
        self.SERVER = server
        self.data = {}
        self.task = SimpleNamespace(command=command)
        self.OpsiFleet_Fleet = fleet
        self.overrides = {}

    def bind(self, name, func_list=None):
        pass

    def override(self, **kwargs):
        self.overrides.update(kwargs)

    def cross_get(self, keys):
        return deep_get(self.data, keys=keys.split('.'))

    def cross_set(self, keys, value):
        deep_set(self.data, keys=keys.split('.'), value=value)


class View(dict):
    center_loca = (5, 4)


def make_handler(command='OpsiHazard1Leveling', hazard=1):
    handler = AkashiRecovery.__new__(AkashiRecovery)
    handler.config = ConfigStub(command=command)
    handler.zone = SimpleNamespace(zone_id=22, hazard_level=hazard, is_port=False)
    handler.is_in_special_zone = Mock(return_value=False)
    handler.is_in_map = Mock(return_value=True)
    handler.camera = (4, 3)
    handler.view = View()
    handler._solved_map_event = set()
    handler._in_forced_recovery = False
    handler._goto_akashi_with_other_fleets = Mock(return_value=False)
    handler.execute_fixed_patrol_scan = Mock()
    handler.map_rescan = Mock()
    return handler


def make_grid(location=(1, 7)):
    return SimpleNamespace(button=(270, 584, 345, 668), location=location, is_akashi=True)


def save_pending(handler, **overrides):
    state = dict(server='cn', reset=get_os_next_reset().isoformat(),
                 zone=22, fleet=1, target=[1, 7])
    state.update(overrides)
    handler.config.cross_set(PENDING_AKASHI_PATH, state)
    return state


def test_remember_creates_record_only_for_cl1():
    handler = make_handler('OpsiHazard1Leveling')
    handler._remember_pending_akashi(make_grid())
    state = handler._get_pending_akashi()
    assert state is not None and state['zone'] == 22 and state['fleet'] == 1

    other = make_handler('OpsiMeowfficerFarming')
    other._remember_pending_akashi(make_grid())
    assert other._get_pending_akashi() is None


def test_remember_keeps_existing_record_and_fills_missing_target():
    handler = make_handler('OpsiHazard1Leveling')
    handler._remember_pending_akashi()  # 无定位的第一次确认
    state = handler._get_pending_akashi()
    assert state['target'] is None
    handler._remember_pending_akashi(make_grid(location=(2, 6)))  # 第二次带定位
    state = handler._get_pending_akashi()
    assert state['target'] == [1, 5]


def test_mismatched_server_reset_zone_or_fleet_invalidates():
    for key, value in (('server', 'jp'), ('reset', 'old'), ('zone', 44), ('fleet', 0)):
        handler = make_handler()
        save_pending(handler, **{key: value})
        assert handler._get_pending_akashi() is None
        handler.map_rescan.assert_not_called()


def test_hazard_other_than_one_invalidates():
    handler = make_handler(hazard=6)
    save_pending(handler)
    assert handler._get_pending_akashi() is None
    handler.map_rescan.assert_not_called()


def test_finish_clears_only_for_cl1():
    handler = make_handler('OpsiHazard1Leveling')
    save_pending(handler)
    handler._finish_pending_akashi()
    assert handler._get_pending_akashi() is None

    other = make_handler('OpsiMeowfficerFarming')
    save_pending(other)
    other._finish_pending_akashi()
    assert other._get_pending_akashi() is not None


def test_resume_rescans_under_cl1_identity_and_recovers_when_missing():
    from module.exception import GameStuckError

    handler = make_handler('OpsiHazard1Leveling')
    save_pending(handler)
    # 视野里没找到明石 → 走已确认恢复：换队失败 → 临时强制移动 →
    # 仍失败则主动抛 GameStuckError 触发重启，续查记录保留（对齐 AzurPilot）。
    try:
        handler._resume_pending_akashi()
        raised = False
    except GameStuckError:
        raised = True
    assert raised
    handler.map_rescan.assert_called_once_with(rescan_mode='full')
    handler.execute_fixed_patrol_scan.assert_called_once_with(akashi_recovery=True)
    assert handler._get_pending_akashi() is not None


def test_resume_ignores_mismatched_records():
    handler = make_handler('OpsiHazard1Leveling')
    save_pending(handler, zone=44)
    assert handler._resume_pending_akashi() is False
    handler.map_rescan.assert_not_called()


# ---------------------------------------------------------------- 紧急强制移动

class PatrolHarness(AkashiRecovery):
    def __init__(self, *, pending, switch=False, command='OpsiHazard1Leveling'):
        self.config = ConfigStub(command=command)
        self.zone = SimpleNamespace(zone_id=22, hazard_level=1, is_port=False)
        self.is_in_special_zone = Mock(return_value=False)
        self.camera = (4, 3)
        self.view = View()
        self.switch = switch
        self.moved = False
        self.map = SimpleNamespace(grids=[1])
        self._solved_map_event = set()
        self._in_forced_recovery = False
        if pending:
            self._remember_pending_akashi(make_grid())
            state = self._get_pending_akashi()
            state['fleet'] = 1
            self.config.cross_set(PENDING_AKASHI_PATH, state)

    execute_fixed_patrol_scan = OSMap.execute_fixed_patrol_scan

    def _forced_move_enabled(self):
        return self.switch

    def _is_meowfficer_task(self):
        return False

    def map_init(self, map_=None):
        pass

    def _move_fleets_and_rescan(self):
        self.moved = True
        self._solved_map_event.add('is_akashi')

    def fleet_set(self, fleet):
        pass

    def clear_question_any_fleet(self):
        return False

    def _read_current_action_point(self):
        return 0

    @property
    def _FIXED_PATROL_L2_AP(self):
        return 7

    @property
    def _question_unreachable(self):
        return False


def test_emergency_bypasses_the_switch_and_goes_straight_to_l2():
    stub = PatrolHarness(pending=True, switch=False)
    assert stub.execute_fixed_patrol_scan(akashi_recovery=True) is True
    assert stub.moved is True


def test_without_pending_the_switch_gate_still_applies():
    stub = PatrolHarness(pending=False, switch=False)
    assert stub.execute_fixed_patrol_scan(akashi_recovery=True) is False
    assert stub.moved is False
