"""每个商店自己的开关（对齐 AzurPilot 的 `RewardShop`：关掉的商店直接跳过，不进店逛一圈）。

覆盖 `ShopOnce`（其他商店）的军需/舰队/核心/勋章四个店，以及 `ShopFrequent`（通用商店）。
"""

from types import SimpleNamespace

import module.shop.shop_reward as shop_reward_mod
from module.shop.shop_reward import RewardShop

SHOPS = {
    'GeneralShop_250814': 'GeneralShop',
    'MeritShop_250814': 'MeritShop',
    'GuildShop_250814': 'GuildShop',
    'CoreShop_250814': 'CoreShop',
    'MedalShop2_250814': 'MedalShop2',
}


class Recorder:
    """替身商店类：只记录有没有被 run()。"""

    def __init__(self, name, ran):
        self.name = name
        self.ran = ran

    def __call__(self, config, device):
        return self

    def run(self):
        self.ran.append(self.name)


class ShopStub:
    run_frequent = RewardShop.run_frequent
    run_once = RewardShop.run_once

    def __init__(self, disabled=()):
        self.ran = []
        self.delayed = []
        config = {'task_delay': self._task_delay}
        for arg in SHOPS.values():
            config[f'{arg}_Enable'] = arg not in disabled
        self.config = SimpleNamespace(**config)
        self.device = SimpleNamespace(click_record_clear=lambda: None)
        self.shop_nav_250814 = SimpleNamespace(set=lambda nav, main=None: None)
        self.shop_tab_250814 = SimpleNamespace(set=lambda tab, main=None: None)

    def _task_delay(self, server_update=False, **kwargs):
        self.delayed.append(server_update)

    def ui_goto_shop(self):
        pass


def run(method, disabled=()):
    saved = {name: getattr(shop_reward_mod, name) for name in SHOPS}
    stub = ShopStub(disabled=disabled)
    for name, arg in SHOPS.items():
        setattr(shop_reward_mod, name, Recorder(arg, stub.ran))
    try:
        getattr(stub, method)()
    finally:
        for name, value in saved.items():
            setattr(shop_reward_mod, name, value)
    return stub


def test_all_shops_on_runs_every_shop():
    stub = run('run_once')
    assert stub.ran == ['MeritShop', 'GuildShop', 'CoreShop', 'MedalShop2']


def test_disabled_shops_are_skipped_in_order():
    """关掉两个店：另外两个照跑，顺序不变（切页动作保留，不会把后面的店带到错的面板）。"""
    stub = run('run_once', disabled=('MeritShop', 'CoreShop'))
    assert stub.ran == ['GuildShop', 'MedalShop2']


def test_disabling_every_shop_still_delays_the_task():
    """全关也不能让任务卡住：task_delay 必须照常执行，否则调度会反复进商店。"""
    stub = run('run_once', disabled=('MeritShop', 'GuildShop', 'CoreShop', 'MedalShop2'))
    assert stub.ran == []
    assert stub.delayed == [True]


def test_frequent_shop_has_its_own_switch():
    stub = run('run_frequent')
    assert stub.ran == ['GeneralShop']
    stub = run('run_frequent', disabled=('GeneralShop',))
    assert stub.ran == []
    assert stub.delayed == [True]
