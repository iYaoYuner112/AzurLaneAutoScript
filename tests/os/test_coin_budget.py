"""黄币预算：读数新鲜度。

黄币靠截图 OCR，慢设备或界面动画没走完时会读到旧值或 0，调度据此判断
"要不要打补给"就会误判。这里锁定三件事：
1. 连续两次一致才算新鲜；
2. OCR 失败沿用上次读数（带时间戳，过期作废），不要把 0 当成余额；
3. 调度决策前读数不新鲜就重读一次。
"""

from time import monotonic
from types import SimpleNamespace

import module.os_handler.os_status as os_status_module
from module.os_handler.os_status import OSStatus


class FakeOcr:
    def __init__(self, values, cycle=False):
        self.values = list(values)
        self.cycle = cycle
        self.calls = 0

    def ocr(self, image):
        self.calls += 1
        if self.cycle:
            return self.values[self.calls % len(self.values)]
        if len(self.values) == 1:
            return self.values[0]
        return self.values.pop(0)


class PatchedOcr:
    """替换模块里的 OCR 与资源记录，用例结束后恢复。"""

    def __init__(self, values, cycle=False):
        self.ocr = FakeOcr(values, cycle=cycle)
        self.records = []

    def __enter__(self):
        self._ocr = os_status_module.OCR_SHOP_YELLOW_COINS
        self._record = os_status_module.record_dashboard_resource
        os_status_module.OCR_SHOP_YELLOW_COINS = self.ocr
        os_status_module.record_dashboard_resource = (
            lambda config, name, value: self.records.append((name, value)))
        return self

    def __exit__(self, *exc):
        os_status_module.OCR_SHOP_YELLOW_COINS = self._ocr
        os_status_module.record_dashboard_resource = self._record
        return False


class StatusStub:
    YELLOW_COINS_MAX_AGE = OSStatus.YELLOW_COINS_MAX_AGE
    _shop_yellow_coins = 0
    _yellow_coins_updated_at = 0.0
    _yellow_coins_fresh = False
    _yellow_coins_source = 'none'

    get_yellow_coins = OSStatus.get_yellow_coins
    get_yellow_coins_for_decision = OSStatus.get_yellow_coins_for_decision
    _record_yellow_coins = OSStatus._record_yellow_coins
    yellow_coins_fresh = OSStatus.yellow_coins_fresh
    yellow_coins_source = OSStatus.yellow_coins_source

    def __init__(self):
        self.config = SimpleNamespace()
        self.device = SimpleNamespace(image=None)
        self.iterations = 0

    def loop(self, *args, **kwargs):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        self.iterations += 1
        if self.iterations > 12:
            raise StopIteration
        return self.iterations


def test_two_equal_readings_are_fresh():
    stub = StatusStub()
    with PatchedOcr([500, 500]):
        assert stub.get_yellow_coins() == 500
    assert stub.yellow_coins_fresh() is True
    assert stub.yellow_coins_source == 'ocr'


def test_different_readings_are_not_fresh():
    """两次读数总是不一样：可以用最后一次，但不能标成新鲜。"""
    stub = StatusStub()
    with PatchedOcr([500, 700], cycle=True):
        value = stub.get_yellow_coins()
    assert value in (500, 700)
    assert stub.yellow_coins_fresh() is False
    assert stub.yellow_coins_source == 'ocr-once'


def test_ocr_failure_reuses_the_recent_reading():
    stub = StatusStub()
    stub._shop_yellow_coins = 12345
    stub._yellow_coins_updated_at = monotonic()
    with PatchedOcr([0, 0, 0]):
        assert stub.get_yellow_coins() == 12345
    assert stub.yellow_coins_source == 'reuse'


def test_expired_reading_is_not_reused():
    """沿用上次值必须带过期时间，不能变成永久缓存。"""
    stub = StatusStub()
    stub._shop_yellow_coins = 12345
    stub._yellow_coins_updated_at = monotonic() - stub.YELLOW_COINS_MAX_AGE - 1
    with PatchedOcr([0, 0, 0]):
        assert stub.get_yellow_coins() == 0
    assert stub.yellow_coins_source == 'none'


def test_no_history_and_no_ocr_is_zero():
    stub = StatusStub()
    with PatchedOcr([0, 0]):
        assert stub.get_yellow_coins() == 0
    assert stub.yellow_coins_fresh() is False


def test_stale_timestamp_makes_a_fresh_reading_unusable():
    stub = StatusStub()
    stub._yellow_coins_fresh = True
    stub._yellow_coins_updated_at = monotonic() - stub.YELLOW_COINS_MAX_AGE - 1
    assert stub.yellow_coins_fresh() is False


def test_decision_reads_again_when_the_first_read_is_not_fresh():
    stub = StatusStub()
    calls = []

    def fake_get_yellow_coins(timeout=3):
        calls.append(1)
        if len(calls) == 1:
            stub._yellow_coins_fresh = False
            return 111
        stub._yellow_coins_fresh = True
        stub._yellow_coins_updated_at = monotonic()
        return 222

    stub.get_yellow_coins = fake_get_yellow_coins
    assert stub.get_yellow_coins_for_decision() == 222
    assert len(calls) == 2


def test_decision_keeps_a_fresh_first_read():
    stub = StatusStub()
    calls = []

    def fake_get_yellow_coins(timeout=3):
        calls.append(1)
        stub._yellow_coins_fresh = True
        stub._yellow_coins_updated_at = monotonic()
        return 333

    stub.get_yellow_coins = fake_get_yellow_coins
    assert stub.get_yellow_coins_for_decision() == 333
    assert len(calls) == 1
