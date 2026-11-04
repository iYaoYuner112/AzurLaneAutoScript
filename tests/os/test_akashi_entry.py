# -*- coding: utf-8 -*-
"""明石入口（对齐 AzurPilot 当前版）。

相邻明石直接进店；点不到时用有界点击 + 等待，失败交给换队/强制移动。
不再使用旧的“安全区夹取 + 挪镜头”路径。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.os.map import OSMap
from module.os_shop.assets import PORT_SUPPLY_CHECK
from module.os_shop.shop import OSShop


class _Ctx:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class StoreStub:
    def __init__(self, results, map_after=True, bar_open=False):
        self.results = list(results)
        self.map_after = map_after
        self.bar_open = bar_open
        self.clicks = []
        self.device = SimpleNamespace(click=self._click)
        self.config = SimpleNamespace(temporary=lambda **kwargs: _Ctx())

    def _click(self, grid):
        self.clicks.append(grid)

    def wait_until_walk_stable(self, **kwargs):
        return self.results.pop(0)

    def _close_fleet_bar_if_open(self):
        return self.bar_open

    def is_in_map(self):
        return self.map_after


def test_open_store_succeeds_after_a_retry():
    stub = StoreStub(['', 'akashi'])
    assert OSMap._open_akashi_store(stub, 'AKASHI') is True
    assert stub.clicks == ['AKASHI', 'AKASHI']


def test_open_store_is_bounded():
    stub = StoreStub(['', '', ''])
    assert OSMap._open_akashi_store(stub, 'AKASHI') is False
    assert stub.clicks == ['AKASHI', 'AKASHI', 'AKASHI']


def test_fleet_bar_click_stops_retrying():
    stub = StoreStub([''], bar_open=True)
    assert OSMap._open_akashi_store(stub, 'AKASHI') is False
    assert stub.clicks == ['AKASHI']


def test_leaving_map_stops_retrying():
    stub = StoreStub([''], map_after=False)
    assert OSMap._open_akashi_store(stub, 'AKASHI') is False
    assert stub.clicks == ['AKASHI']


class ShopStub:
    def __init__(self):
        self.ui_click_args = None
        self.bought = 0
        self.backed = 0

    def is_in_map(self):
        return True

    def handle_story_skip(self):
        return False

    def ui_click(self, click_button, **kwargs):
        self.ui_click_args = (click_button, kwargs)

    def os_shop_buy(self, select_func):
        self.bought += 1

    def os_shop_get_item_to_buy_in_akashi(self):
        return None

    def ui_back(self, **kwargs):
        self.backed += 1


def test_handle_uses_direct_ui_click_for_ap_current_behavior():
    stub = ShopStub()
    OSShop.handle_akashi_supply_buy(stub, 'AKASHI')
    button, kwargs = stub.ui_click_args
    assert button == 'AKASHI'
    assert kwargs['appear_button'] == stub.is_in_map
    assert kwargs['check_button'] == PORT_SUPPLY_CHECK
    assert stub.bought == 1
    assert stub.backed == 1


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
