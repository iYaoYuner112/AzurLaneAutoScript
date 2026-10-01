"""资源监视器核心逻辑单测。

覆盖 spec 的 Test 1–8：正常读取、消耗、增加、OCR 失败保留旧值、OCR 抖动、
异常值拒绝、stop 后不再产生事件、回调异常隔离；以及历史记录与事件通知。
"""

from datetime import datetime

from module.statistics.resource_monitor import (
    RESOURCE_COIN,
    RESOURCE_OIL,
    ChangeDetector,
    ResourceChangedEvent,
    ResourceMonitor,
)


def make_monitor(**kwargs):
    monitor = ResourceMonitor(**kwargs)
    monitor.start()
    return monitor


def read(monitor, name, value, source='ocr', now=None):
    """模拟连续两次读到同一值（collector 的 stable_read 行为）。"""
    first = monitor.submit(name, value, source=source, now=now)
    second = monitor.submit(name, value, source=source, now=now)
    return second  # 确认事件（或 None）


# ---- Test 1: 正常读取 ----

def test_normal_read_sets_state():
    monitor = make_monitor()
    event = read(monitor, RESOURCE_OIL, 12345)
    assert event is not None
    assert monitor.get(RESOURCE_OIL) == 12345
    assert monitor.state.valid is True


# ---- Test 2: 资源消耗 ----

def test_resource_consumption_delta_negative():
    monitor = make_monitor()
    read(monitor, RESOURCE_OIL, 12345)
    event = read(monitor, RESOURCE_OIL, 12200)
    assert event.delta == -145
    assert event.old_value == 12345
    assert event.new_value == 12200
    assert monitor.get(RESOURCE_OIL) == 12200


# ---- Test 3: 资源增加 ----

def test_resource_gain_delta_positive():
    monitor = make_monitor()
    read(monitor, RESOURCE_OIL, 12345)
    event = read(monitor, RESOURCE_OIL, 12500)
    assert event.delta == 155
    assert monitor.get(RESOURCE_OIL) == 12500


# ---- Test 4: OCR 失败保留旧值 ----

def test_ocr_failure_keeps_old_value():
    monitor = make_monitor()
    read(monitor, RESOURCE_OIL, 12345)
    # OCR 失败：submit(None) 不覆盖状态
    result = monitor.submit(RESOURCE_OIL, None)
    assert result is None
    assert monitor.get(RESOURCE_OIL) == 12345


# ---- Test 5: OCR 抖动不误判 ----

def test_ocr_jitter_does_not_emit_change():
    monitor = make_monitor()
    read(monitor, RESOURCE_OIL, 12345)
    # 12346 是抖动：只读到一次，未确认
    assert monitor.submit(RESOURCE_OIL, 12346) is None
    # 又回到 12345：12346 未确认即被放弃，不产生变化事件
    read(monitor, RESOURCE_OIL, 12345)
    assert monitor.get(RESOURCE_OIL) == 12345
    # 历史里只有第一次 12345 的初始化事件，没有 12346 相关变化
    assert all(e.new_value != 12346 for e in monitor.get_history())


# ---- Test 6: 异常数值拒绝写入 ----

def test_abnormal_value_rejected():
    monitor = make_monitor()
    read(monitor, RESOURCE_OIL, 12345)
    # 999999999 超过上限，拒绝
    result = read(monitor, RESOURCE_OIL, 999999999)
    assert result is None
    assert monitor.get(RESOURCE_OIL) == 12345


def test_negative_value_rejected():
    monitor = make_monitor()
    read(monitor, RESOURCE_OIL, 12345)
    assert read(monitor, RESOURCE_OIL, -5) is None
    assert monitor.get(RESOURCE_OIL) == 12345


# ---- Test 7: stop 后不再产生事件 ----

def test_stop_stops_processing():
    monitor = make_monitor()
    read(monitor, RESOURCE_OIL, 12345)
    monitor.stop()
    assert monitor.running is False
    # stop 后 submit 直接忽略，不产生事件、不更新状态
    assert read(monitor, RESOURCE_OIL, 12200) is None
    assert monitor.get(RESOURCE_OIL) == 12345


def test_pause_stops_processing_and_resume_recovers():
    monitor = make_monitor()
    read(monitor, RESOURCE_OIL, 12345)
    monitor.pause()
    assert read(monitor, RESOURCE_OIL, 12200) is None
    monitor.resume()
    event = read(monitor, RESOURCE_OIL, 12200)
    assert event is not None
    assert event.delta == -145


# ---- Test 8: 回调异常隔离 ----

def test_listener_exception_is_isolated():
    monitor = make_monitor()

    def bad_listener(event):
        raise RuntimeError('listener boom')

    def good_listener(event):
        good_listener.called = True

    good_listener.called = False
    monitor.on_change(bad_listener)
    monitor.on_change(good_listener)

    # 回调异常不能影响 submit 和后续回调
    event = read(monitor, RESOURCE_OIL, 12345)
    assert event is not None
    assert good_listener.called is True
    assert monitor.get(RESOURCE_OIL) == 12345


# ---- 事件通知 ----

def test_on_change_fires_with_event():
    monitor = make_monitor()
    seen = []
    monitor.on_change(seen.append)
    read(monitor, RESOURCE_OIL, 12345)
    assert len(seen) == 1
    assert isinstance(seen[0], ResourceChangedEvent)
    assert seen[0].resource == RESOURCE_OIL
    assert seen[0].new_value == 12345


# ---- 历史记录 ----

def test_history_records_changes():
    monitor = make_monitor()
    read(monitor, RESOURCE_OIL, 12345)   # 首次读入：delta=0（无旧值）
    read(monitor, RESOURCE_OIL, 12200)   # 变化：delta=-145
    read(monitor, RESOURCE_COIN, 5000)   # 首次读入：delta=0
    assert len(monitor.get_history()) == 3
    oil_events = monitor.get_history(RESOURCE_OIL)
    assert len(oil_events) == 2
    assert [e.delta for e in oil_events] == [0, -145]


def test_history_is_bounded():
    monitor = make_monitor(history_maxlen=3)
    for value in range(10, 20):
        read(monitor, RESOURCE_OIL, value)
    assert len(monitor.get_history()) == 3


# ---- ChangeDetector 单独行为 ----

def test_detector_requires_consecutive_reads():
    detector = ChangeDetector(confirm_reads=3)
    assert detector.submit('oil', 100) is None
    assert detector.submit('oil', 100) is None
    assert detector.submit('oil', 100) == 100


def test_detector_rejects_non_consecutive():
    detector = ChangeDetector(confirm_reads=2)
    detector.submit('oil', 100)
    detector.submit('oil', 101)  # 不一致，重置
    assert detector.submit('oil', 100) is None
