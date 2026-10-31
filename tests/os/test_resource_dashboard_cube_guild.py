"""总览页新增的两项资源：心智魔方（Cube）与舰队币（GuildCoin）。

覆盖：
1. 目录里有这两项，且图标文件存在（缺图标不会报错，但会缺图）
2. 舰队商店读到余额时会上报
3. 建造任务里保留上报调用（防止被误删）
"""

import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.gacha.gacha_reward import RewardGacha
from module.shop import shop_status
from module.statistics.resource_monitor import DASHBOARD_RESOURCES

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ICON_DIR = os.path.join(REPO, 'assets', 'gui', 'icon', 'resource')


class FakeConfig:
    def __init__(self):
        self.resources = {}
        self.modified = {}

    def cross_get(self, keys, default=None):
        return self.resources.get('m', default)

    def save(self):
        self.resources['m'] = self.modified['Alas.Storage.Storage.ResourceMonitor']
        self.modified.clear()


def test_catalog_has_cube_and_guild_coin():
    keys = [r.key for r in DASHBOARD_RESOURCES]
    assert 'Cube' in keys, keys
    assert 'GuildCoin' in keys, keys
    # 顺序与 AzurPilot 总览页一致：魔方在第 4 张，舰队币在最后
    assert keys.index('Cube') == 3, keys
    assert keys[-1] == 'GuildCoin', keys


def test_icon_files_exist_for_new_resources():
    for item in DASHBOARD_RESOURCES:
        if item.key not in ('Cube', 'GuildCoin'):
            continue
        path = os.path.join(ICON_DIR, item.icon + '.png')
        assert os.path.isfile(path), f'缺少图标: {path}'


def test_guild_coin_recorded_when_status_read():
    config = FakeConfig()
    stub = SimpleNamespace(config=config, device=SimpleNamespace(image=None))

    with patch.object(shop_status.OCR_SHOP_GUILD_COINS, 'ocr', return_value=5500):
        amount = shop_status.ShopStatus.status_get_guild_coins(stub)

    assert amount == 5500
    # FakeConfig.save() 会把 modified 落进 resources，这里读落盘后的值
    assert config.resources['m']['GuildCoin']['Value'] == 5500


def test_gacha_keeps_cube_report():
    source = inspect.getsource(RewardGacha.gacha_run)
    assert "record_dashboard_resource(self.config, 'Cube'" in source, \
        '建造任务里的魔方上报被删了'
