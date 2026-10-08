"""雷达问号扫描窗（对齐 AzurPilot）。

事件图标被舰队模型 / 装置挡住时，大图的模板匹配会闪断，雷达小地图上的白色问号
是纯颜色检测、稳定得多，是这种情况下唯一可靠的来源。窗口窄一格，就是漏判一格。
"""

from types import SimpleNamespace

from module.os.radar import Radar

# AzurPilot `Radar.predict_question` 扫描的偏移，顺序也照抄。
AZURPILOT_OFFSETS = [
    (0, 1), (-1, 0), (1, 0), (0, -1),
    (1, 1), (-1, 1), (1, -1), (-1, -1),
    (0, -2), (0, -3),
    (-2, 0), (2, 0), (0, 2),
    (-3, 0), (3, 0),
]


def make_radar():
    """跳过截图识别，只按格子标志回答。"""
    radar = Radar(config=SimpleNamespace(MAP_HAS_SIREN=False))
    radar.show = lambda: None
    radar.predict = lambda image: None
    for grid in radar:
        grid.is_question = False
        grid.is_port = False
    return radar


def mark(radar, location, **flags):
    grid = radar[location]
    for key, value in flags.items():
        setattr(grid, key, value)


def test_every_azurpilot_offset_is_scanned():
    """AP 扫的每个偏移，单独放一个问号都应该找得到。"""
    for offset in AZURPILOT_OFFSETS:
        radar = make_radar()
        mark(radar, offset, is_question=True)
        assert radar.predict_question(None, in_port=True) == offset, offset


def test_near_offsets_win_over_far_ones():
    """同时有多个问号时按近距优先返回。"""
    radar = make_radar()
    mark(radar, (0, -3), is_question=True)
    mark(radar, (0, -2), is_question=True)
    assert radar.predict_question(None) == (0, -2)

    radar = make_radar()
    mark(radar, (3, 0), is_question=True)
    mark(radar, (-1, 0), is_question=True)
    assert radar.predict_question(None) == (-1, 0)


def test_far_below_is_not_scanned():
    """正下方 3 格超出本地视野，转换不成可点格子，扫了只耗重试。"""
    radar = make_radar()
    mark(radar, (0, 3), is_question=True)
    assert radar.predict_question(None) is None


def test_in_port_switch_applies_to_the_whole_window():
    """in_port=False 时港口当问号，新加的偏移同样适用。"""
    radar = make_radar()
    mark(radar, (2, 0), is_port=True)
    assert radar.predict_question(None, in_port=False) == (2, 0)

    radar = make_radar()
    mark(radar, (2, 0), is_port=True)
    assert radar.predict_question(None, in_port=True) is None
