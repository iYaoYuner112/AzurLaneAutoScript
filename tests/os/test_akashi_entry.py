"""明石入口加固：点击避让固定 UI、误入页面恢复、失败不再算已处理。

明石常刷新在地图边缘，和左下角舰队选择栏、右侧按钮重合：点击会命中 UI，
商店没开而事件却被标成"已处理"，这只猫就永久漏掉了。这里锁定四件事：
1. 点击框夹进地图安全区，整块被压住时不点；
2. 完全被压住时挪镜头重新识别，最多 2 次；
3. 误入别的页面能退回来；
4. handle_akashi_supply_buy 返回 bool：只有"进店 + 买完 + 回到地图"才 True。
"""

from types import SimpleNamespace

from module.base.button import Button
from module.os_shop import shop as shop_module
from module.os_shop.shop import OSShop


class GridStub:
    """带点击框的事件格；box=None 表示这个对象没有 button 属性。"""

    is_akashi = True

    def __init__(self, box=None):
        if box is not None:
            self.button = box

    def __str__(self):
        return 'AKASHI_GRID'


class ShopStub:
    akashi_safe_click_button = staticmethod(OSShop.akashi_safe_click_button)
    akashi_recover_page = OSShop.akashi_recover_page
    akashi_prepare_click = OSShop.akashi_prepare_click
    akashi_enter_shop = OSShop.akashi_enter_shop
    handle_akashi_supply_buy = OSShop.handle_akashi_supply_buy

    def __init__(self, shop_open=False, entered=True, back_to_map=True, page=None,
                 bar_opened=False, grid_after_focus=None, storefront=(500, 300, 560, 360)):
        self.shop_open = shop_open
        self._entered = entered
        self._back_to_map = back_to_map
        self._page = page
        self._bar_opened = bar_opened
        self._grid_after_focus = grid_after_focus
        self.storefront = storefront

        self.buys = 0
        self.clicks = 0
        self.screenshots = 0
        self.quits = []
        self.focus_calls = 0
        self.story_skips = 0

        self.device = SimpleNamespace(screenshot=self._screenshot, click=self._click)
        self.view = SimpleNamespace(select=self._select)
        self.fleet_selector = SimpleNamespace(
            bar_opened=lambda: self._bar_opened, close=self._close_bar)
        if entered is not None:
            self.akashi_enter_shop = self._enter_shop

    # ---- UI 替身 ----
    def _screenshot(self):
        self.screenshots += 1

    def _click(self, button):
        self.clicks += 1

    def _select(self, **kwargs):
        if kwargs.get('is_akashi') and self._grid_after_focus is not None:
            return [self._grid_after_focus]
        return []

    def _close_bar(self):
        self.quits.append('fleet_bar')
        self._bar_opened = False

    def _enter_shop(self, button):
        self.clicks += 1
        self.shop_open = self._entered
        return self._entered

    # ---- 页面判定替身 ----
    def appear(self, button, offset=(0, 0)):
        return self.shop_open

    def is_in_map(self):
        return self._back_to_map

    def is_in_globe(self):
        return self._page == 'globe'

    def os_globe_goto_map(self):
        self.quits.append('globe')

    def is_in_storage(self):
        return self._page == 'storage'

    def storage_quit(self):
        self.quits.append('storage')

    def is_in_os_mission(self):
        return self._page == 'mission'

    def os_mission_quit(self):
        self.quits.append('mission')

    def is_in_map_order(self):
        return self._page == 'order'

    def order_quit(self):
        self.quits.append('order')

    def handle_story_skip(self):
        self.story_skips += 1
        return False

    def interval_clear(self, buttons):
        pass

    def focus_to(self, grid, swipe_limit=None):
        self.focus_calls += 1

    def update(self):
        pass

    def os_shop_buy(self, select_func):
        self.buys += 1
        return 1

    def os_shop_get_item_to_buy_in_akashi(self):
        return None

    def ui_back(self, **kwargs):
        pass


def test_partially_covered_box_is_clamped_into_the_safe_area():
    grid = GridStub(box=(200, 100, 400, 300))
    button = OSShop.akashi_safe_click_button(grid)
    assert button is not None
    assert button.button == (239, 128, 400, 300)


def test_box_inside_the_safe_area_is_kept_as_is():
    grid = GridStub(box=(500, 300, 560, 360))
    button = OSShop.akashi_safe_click_button(grid)
    assert button.button == (500, 300, 560, 360)


def test_fully_covered_box_is_not_clickable():
    # 整块落在地图安全区左边（舰队选择栏那一带）
    assert OSShop.akashi_safe_click_button(GridStub(box=(0, 300, 100, 400))) is None
    # 只压到一点点，夹完小于 6px
    assert OSShop.akashi_safe_click_button(GridStub(box=(230, 128, 242, 200))) is None


def test_object_without_click_box_does_not_crash():
    assert OSShop.akashi_safe_click_button(GridStub()) is None
    assert OSShop.akashi_safe_click_button(object()) is None


def test_prepare_click_moves_camera_twice_then_gives_up():
    """重新识别到了明石、但点击框仍然被压住：只挪两次镜头，不无限重试。"""
    covered = GridStub(box=(0, 300, 100, 400))
    stub = ShopStub(grid_after_focus=covered)
    assert stub.akashi_prepare_click(covered) is None
    assert stub.focus_calls == shop_module.AKASHI_FOCUS_RETRIES


def test_prepare_click_reidentifies_akashi_after_moving_camera():
    stub = ShopStub(grid_after_focus=GridStub(box=(500, 300, 560, 360)))
    button = stub.akashi_prepare_click(GridStub(box=(0, 300, 100, 400)))
    assert button is not None
    assert button.button == (500, 300, 560, 360)
    assert stub.focus_calls == 1


def test_success_needs_shop_and_map():
    stub = ShopStub()
    assert stub.handle_akashi_supply_buy(GridStub(box=(500, 300, 560, 360))) is True
    assert stub.buys == 1


def test_unclickable_akashi_never_buys():
    stub = ShopStub()
    assert stub.handle_akashi_supply_buy(GridStub(box=(0, 300, 100, 400))) is False
    assert stub.buys == 0
    assert stub.clicks == 0


def test_shop_that_never_opens_never_buys():
    stub = ShopStub(entered=False)
    assert stub.handle_akashi_supply_buy(GridStub(box=(500, 300, 560, 360))) is False
    assert stub.buys == 0


def test_not_back_to_the_map_is_a_failure():
    stub = ShopStub(entered=True, back_to_map=False)
    assert stub.handle_akashi_supply_buy(GridStub(box=(500, 300, 560, 360))) is False
    assert stub.buys == 1          # 买了，但没回到地图就不能算处理完


def test_already_in_the_shop_skips_the_click():
    stub = ShopStub(shop_open=True)
    assert stub.handle_akashi_supply_buy(GridStub(box=(500, 300, 560, 360))) is True
    assert stub.buys == 1
    assert stub.clicks == 0


def test_recover_page_leaves_each_wrong_page():
    for page in ('globe', 'storage', 'mission', 'order'):
        stub = ShopStub(page=page)
        assert stub.akashi_recover_page() is True, page
        assert stub.quits == [page], page


def test_recover_page_keeps_the_map_alone():
    stub = ShopStub()
    assert stub.akashi_recover_page() is False
    assert stub.quits == []


def test_recover_page_closes_the_fleet_bar():
    stub = ShopStub(bar_opened=True)
    assert stub.akashi_recover_page() is True
    assert stub.quits == ['fleet_bar']


def test_enter_shop_gives_up_after_the_timeout():
    """点不开就带着超时退回，而不是像 ui_click 那样一直点到设备防连点。"""
    stub = ShopStub(entered=None)
    stub.appear = lambda button, offset=(0, 0): False
    original = shop_module.AKASHI_ENTER_TIMEOUT
    shop_module.AKASHI_ENTER_TIMEOUT = 0
    try:
        assert stub.akashi_enter_shop(Button(area=(500, 300, 560, 360), color=(0, 0, 0),
                                             button=(500, 300, 560, 360))) is False
    finally:
        shop_module.AKASHI_ENTER_TIMEOUT = original
    assert stub.screenshots > 1
