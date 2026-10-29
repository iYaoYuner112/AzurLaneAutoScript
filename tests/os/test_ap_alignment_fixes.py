"""对齐 AP 的两项修复（回归用例）：

1. 大世界商店的「每月 X 号前不运行」开关
2. 隐秘海域按配置跳过侵蚀 2 的坐标
"""

import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.config.config import TaskEnd
from module.os.tasks.shop import OpsiShop
from module.os_handler import storage as storage_mod
from module.os_handler.storage import StorageHandler


class _Stop(Exception):
    """中断桩函数调用链用。"""


class FakeConfig:
    def __init__(self, **options):
        self.__dict__.update(options)
        self.delays = []
        self.stopped = False

    def task_delay(self, **kwargs):
        self.delays.append(kwargs)

    def task_stop(self):
        self.stopped = True
        raise TaskEnd


# ---------------------------------------------------------------- 商店

def _make_shop(disable_before):
    config = FakeConfig(OpsiShop_DisableBeforeDate=disable_before)
    stub = SimpleNamespace(config=config, zone=SimpleNamespace(is_azur_port=True), entered=False)

    def port_enter():
        stub.entered = True
        raise _Stop()

    stub.port_enter = port_enter
    return stub


def test_shop_skips_before_configured_day():
    """限制日期设成今天 → 今天应推迟到服务器刷新并停止（不写死日期，按当天算）。"""
    stub = _make_shop(datetime.now().day)
    try:
        OpsiShop.os_shop(stub)
    except TaskEnd:
        pass
    else:
        raise AssertionError('应当 task_stop 结束本轮')
    assert stub.config.delays == [{'server_update': True}], stub.config.delays
    assert stub.entered is False, '不该再进港口'


def test_shop_without_limit_still_runs():
    """限制为 0（默认）时不拦，照常进港口。"""
    stub = _make_shop(0)
    try:
        OpsiShop.os_shop(stub)
    except _Stop:
        pass
    assert stub.config.stopped is False
    assert stub.entered is True


# ------------------------------------------------------- 隐秘跳过侵蚀 2

def _make_storage(buttons):
    stub = SimpleNamespace(device=SimpleNamespace(image=object()), loop=lambda: [0])
    picked = []

    def checkout(button, types=None):
        picked.append(button)
        return True

    stub._storage_coordinate_checkout = checkout
    stub._storage_item_to_template = lambda item: SimpleNamespace(
        match_multi=lambda image, similarity=0.75: buttons)
    stub.storage_quit = lambda: None
    stub._storage_obscure_is_hazard_2 = lambda image, button: getattr(button, 'hazard2', False)
    return stub, picked


def _run_checkout(stub, item, skip_hazard_2):
    original_scroll = storage_mod.SCROLL_STORAGE
    original_gray = storage_mod.rgb2gray
    storage_mod.SCROLL_STORAGE = SimpleNamespace(appear=lambda main=None: False)
    storage_mod.rgb2gray = lambda image: image
    try:
        return StorageHandler.storage_checkout_item(
            stub, item, skip_obscure_hazard_2=skip_hazard_2)
    finally:
        storage_mod.SCROLL_STORAGE = original_scroll
        storage_mod.rgb2gray = original_gray


def test_obscure_skips_hazard_2_when_enabled():
    """开启跳过时：第一个是侵蚀2 → 取第二个。"""
    h2 = SimpleNamespace(area=(0, 0, 10, 10), hazard2=True)
    h3 = SimpleNamespace(area=(0, 20, 10, 30), hazard2=False)
    stub, picked = _make_storage([h2, h3])

    assert _run_checkout(stub, 'OBSCURE', True) is True
    assert picked == [h3], f'应当跳过侵蚀2，实际取了 {picked}'


def test_obscure_keeps_hazard_2_when_disabled():
    """关闭跳过时（默认）：第一个就是侵蚀2 也照取。"""
    h2 = SimpleNamespace(area=(0, 0, 10, 10), hazard2=True)
    h3 = SimpleNamespace(area=(0, 20, 10, 30), hazard2=False)
    stub, picked = _make_storage([h2, h3])

    assert _run_checkout(stub, 'OBSCURE', False) is True
    assert picked == [h2], f'默认不该跳过侵蚀2，实际取了 {picked}'


def test_hazard_2_marker_uses_ap_crop_window():
    """角标在图标左上：必须用灰度图 + (-25,-35) 偏移 + 0.92 相似度（与 AP 一致）。"""
    calls = {}

    class FakeTemplate:
        @staticmethod
        def match(image, similarity=0.85):
            calls['similarity'] = similarity
            return True

    orig = (storage_mod.crop, storage_mod.area_offset,
            storage_mod.TEMPLATE_STORAGE_OBSCURE_HAZARD_2)
    storage_mod.area_offset = lambda area, offset: ('shifted', area, offset)

    def fake_crop(image, area, copy=True):
        calls['crop'] = (image, area, copy)
        return 'cropped'

    storage_mod.crop = fake_crop
    storage_mod.TEMPLATE_STORAGE_OBSCURE_HAZARD_2 = FakeTemplate()
    try:
        matched = StorageHandler._storage_obscure_is_hazard_2(
            SimpleNamespace(), 'GRAY_IMAGE', SimpleNamespace(area=(10, 20, 30, 40)))
    finally:
        (storage_mod.crop, storage_mod.area_offset,
         storage_mod.TEMPLATE_STORAGE_OBSCURE_HAZARD_2) = orig

    assert matched is True
    image, area, copy = calls['crop']
    assert image == 'GRAY_IMAGE', '必须在灰度图上裁剪'
    assert area == ('shifted', (10, 20, 30, 40), (-25, -35)), f'裁剪偏移不对: {area}'
    assert copy is False
    assert calls['similarity'] == 0.92, f'相似度不对: {calls["similarity"]}'


if __name__ == '__main__':
    test_shop_skips_before_configured_day()
    test_shop_without_limit_still_runs()
    test_obscure_skips_hazard_2_when_enabled()
    test_obscure_keeps_hazard_2_when_disabled()
    test_hazard_2_marker_uses_ap_crop_window()
    print('all passed')
