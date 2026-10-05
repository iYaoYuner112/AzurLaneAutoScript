"""计划作战确认：第二层「强制召回指挥喵」弹窗必须还能被点掉，但不能提前放行。

实例实测（1280x720 原始帧，海域里有指挥喵在搜寻时开始计划作战）：
- 标题条 `STRATEGIC_SEARCH_POPUP_CHECK` 定义色 (150,170,209)，召回弹窗实测 (97,135,179)，容差 53；
- 取消键 `POPUP_CANCEL` 定义色 (196,198,199)，实测 (187,188,189)，容差 10（压线）；
- 确定键 `POPUP_CONFIRM` 定义色 (153,183,222)，实测 (145,178,218)，容差 8。

旧逻辑拿标题条当前置条件，第二层弹窗永远认不出：60 秒干等 -> GameStuckError -> 重启 ->
同一状态再撞 -> 三次后停机等人工。现在改成两段式：前 3 帧仍只认标题条（与原来一致），
只有严格判定已经失败、原本注定空转到卡死之后，才允许按「取消+确定」按钮对放宽点掉。
"""

from types import SimpleNamespace

from module.handler.info_handler import InfoHandler
from module.os_handler.strategic import StrategicSearchHandler


class ConfirmLoopStub:
    """按帧脚本跑 `strategic_search_confirm`，记录每一帧都做了哪种判定。"""

    strategic_search_confirm = StrategicSearchHandler.strategic_search_confirm

    def __init__(self, title_bar_ok_frames=(), fallback_ok_frames=(), in_map_from=99):
        self.title_bar_ok = set(title_bar_ok_frames)
        self.fallback_ok = set(fallback_ok_frames)
        self.in_map_from = in_map_from
        self.calls = []

    def loop(self):
        for _ in range(12):
            yield

    def appear(self, button, offset=0, interval=0, similarity=0.85, threshold=10):
        self.frame = sum(1 for c in self.calls if c[0] == 'title_bar') + 1
        self.calls.append(('title_bar', threshold, self.frame))
        return self.frame in self.title_bar_ok

    def handle_popup_confirm(self, name='', offset=None, interval=2, threshold=10):
        frame = getattr(self, 'frame', 0)
        self.calls.append(('popup', threshold, frame))
        if threshold == 10:
            return frame in self.title_bar_ok
        return frame in self.fallback_ok

    def is_in_map(self):
        frame = getattr(self, 'frame', 0)
        self.calls.append(('in_map', None, frame))
        return frame >= self.in_map_from


def popup_frames(stub, threshold):
    return [c[2] for c in stub.calls if c[0] == 'popup' and c[1] == threshold]


def test_normal_flow_never_uses_the_loosened_path():
    """标题条认得出时，行为和原来完全一样：只走严格判定，绝不出现放宽的那一路。"""
    stub = ConfirmLoopStub(title_bar_ok_frames={1}, in_map_from=2)
    assert stub.strategic_search_confirm() is True
    assert popup_frames(stub, 10) == [1]
    assert popup_frames(stub, 20) == []


def test_loosened_path_waits_for_the_strict_one_to_fail():
    """前三帧不许放宽：那是严格判定本该成功的窗口，提前放行才会误点别的弹窗。"""
    stub = ConfirmLoopStub()
    assert stub.strategic_search_confirm() is None
    assert popup_frames(stub, 20) == list(range(4, 13))      # 从第 4 帧才开始试，一路到循环结束
    assert popup_frames(stub, 10) == []                      # 标题条没认出来，严格那路一次都没调


def test_recall_popup_gets_confirmed_then_the_round_continues():
    """召回弹窗：第 4 帧兜底点掉，下一帧回到地图就正常返回。"""
    stub = ConfirmLoopStub(fallback_ok_frames={4}, in_map_from=5)
    assert stub.strategic_search_confirm() is True
    assert 4 in popup_frames(stub, 20)
    assert [c[2] for c in stub.calls if c[0] == 'in_map'][-1] == 5


class PopupStub:
    """按实测容差模拟颜色判定：召回弹窗压在确认弹窗之上时整层偏暗。"""

    _popup_offset = (20, 20)
    needed = {'POPUP_CANCEL': 14, 'POPUP_CONFIRM': 8, 'POPUP_CONFIRM_WHITE': 99}

    def __init__(self):
        self.clicks = []
        self.device = SimpleNamespace(click=lambda button: self.clicks.append(button.name))

    def appear(self, button, offset=0, interval=0, similarity=0.85, threshold=10):
        # 必须两个键都匹配才算数，所以这里按各自需要的容差分别判
        return threshold >= self.needed.get(button.name, 99)

    handle_popup_confirm = InfoHandler.handle_popup_confirm


def test_default_threshold_still_misses_the_darkened_recall_popup():
    """默认阈值 10 下取消键差 14 就认不出 -> 什么都不会点（不是盲点）。"""
    stub = PopupStub()
    assert stub.handle_popup_confirm(name='STRATEGIC_SEARCH') is False
    assert stub.clicks == []


def test_loosened_threshold_confirms_without_blind_clicking():
    stub = PopupStub()
    assert stub.handle_popup_confirm(name='STRATEGIC_SEARCH', threshold=20) is True
    assert stub.clicks == ['POPUP_CONFIRM_STRATEGIC_SEARCH']


def test_button_name_is_restored_after_the_click():
    """临时改名必须还原，否则后续所有弹窗日志都会带上前一次的标记。"""
    stub = PopupStub()
    stub.handle_popup_confirm(name='STRATEGIC_SEARCH', threshold=20)
    stub.handle_popup_confirm(name='GOTO_GLOBE', threshold=20)
    assert stub.clicks == ['POPUP_CONFIRM_STRATEGIC_SEARCH', 'POPUP_CONFIRM_GOTO_GLOBE']
