# -*- coding: utf-8 -*-
"""商店连续跳过买不起物品时不再重复读货币的回归测试。"""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.os_shop.shop import OSShop


class ItemStub:
    def __init__(self, name, price):
        self.name = name
        self.price = price
        self.count = 1
        self.cost = 'Coins'
        self.shop_index = 0
        self.scroll_pos = 0.0


class ShopStub:
    def __init__(self, items):
        self.items = items
        self.items_by_name = {item.name: item for item in items}
        self.reads = 0
        self.bought = []
        self.device = types.SimpleNamespace(click_record=set())
        self.config = types.SimpleNamespace(cross_set=lambda *args, **kwargs: None)

    def scan_all(self):
        return list(self.items)

    def items_filter_in_os_shop(self, items):
        return items

    def os_shop_get_coins(self):
        self.reads += 1

    def get_currency_coins(self, item):
        return 100

    def is_coins_both_not_enough(self):
        return False

    def os_shop_side_navbar_ensure(self, upper):
        pass

    def os_shop_get_items_to_buy(self, name, price):
        return self.items_by_name[name]

    def check_item_count(self, item):
        return True

    def os_shop_buy_execute(self, item):
        self.bought.append(item.name)
        return True


class ShopSkipCoinsTest(unittest.TestCase):
    def test_skipping_after_purchase_does_not_reread_coins(self):
        items = [
            ItemStub('buy_first', 10),
            ItemStub('unaffordable', 1000),
            ItemStub('buy_last', 10),
        ]
        stub = ShopStub(items)
        with patch('module.os_shop.shop.OS_SHOP_SCROLL') as scroll:
            scroll.set.return_value = None
            OSShop.handle_port_supply_buy(stub)

        self.assertEqual(stub.bought, ['buy_first', 'buy_last'])
        # 初始读一次，买完第一件后读一次；跳过买不起的物品后不再重复读。
        self.assertEqual(stub.reads, 2)


if __name__ == '__main__':
    unittest.main()
