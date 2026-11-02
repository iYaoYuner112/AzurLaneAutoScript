"""计划作战确认：第二层「强制召回指挥喵」弹窗的处理（对齐 AzurPilot 857f6dbbc）。

实例实测（1280x720 原始帧，海域里有指挥喵在搜寻时开始计划作战）：
- 标题条 `STRATEGIC_SEARCH_POPUP_CHECK` 定义色 (150,170,209)，召回弹窗实测 (97,135,179)，容差
  53 —— 被召回弹窗压住后标题条认不出，所以召回确认必须靠 `confirmed` 门控来接手。
- 取消键 `POPUP_CANCEL` 定义色 (196,198,199)，实测 (187,188,189)，容差 10（压线）；
- 确定键 `POPUP_CONFIRM` 定义色 (153,183,222)，实测 (145,178,218)，容差 8。

当前采用 AP 的结构：`confirmed` 门控 + 具名检查，默认阈值调用。
已知的接受风险：召回弹窗整体偏暗时取消键容差压线，默认阈值下 `handle_popup_confirm`
可能返回 False；若实机复现卡死，参考历史提交 f3d6fe822 的放宽阈值兜底方案。
"""

from types import SimpleNamespace

from module.handler.info_handler import InfoHandler
from module.os_handler.strategic import StrategicSearchHandler


class ConfirmLoopStub:
    """按帧脚本跑 `strategic_search_confirm`，记录每一帧都做了哪种判定。"""

    strategic_search_confirm = StrategicSearchHandler.strategic_search_confirm

    def __init__(self, title_bar_ok_frames=(), meow_ok_frames=(), in_map_from=99):
        self.title_bar_ok = set(title_bar_ok_frames)
        self.meow_ok = set(meow_ok_frames)
        self.in_map_from = in_map_from
        self.calls = []

    def loop(self):
        for _ in range(12):
            yield

    def appear(self, button, offset=0, interval=0, similarity=0.85, threshold=10):
        self.frame = sum(1 for c in self.calls if c[0] == 'title_bar') + 1
        self.calls.append(('title_bar', self.frame))
        return self.frame in self.title_bar_ok

    def handle_popup_confirm(self, name='', offset=None, interval=2):
        frame = getattr(self, 'frame', 0)
        self.calls.append((name, frame))
        if name == 'STRATEGIC_SEARCH':
            return frame in self.title_bar_ok
        if name == 'STRATEGIC_SEARCH_MEOWFFICER':
            return frame in self.meow_ok
        return False

    def is_in_map(self):
        frame = getattr(self, 'frame', 0)
        self.calls.append(('in_map', frame))
        return frame >= self.in_map_from


def popup_frames(stub, name):
    return [c[1] for c in stub.calls if c[0] == name]


def test_normal_flow_confirms_then_returns_in_map():
    """标题条认得出 -> 确认 -> 下一帧回地图；召回检查只在确认之后才运行。"""
    stub = ConfirmLoopStub(title_bar_ok_frames={1}, in_map_from=2)
    assert stub.strategic_search_confirm() is True
    assert popup_frames(stub, 'STRATEGIC_SEARCH') == [1]
    assert popup_frames(stub, 'STRATEGIC_SEARCH_MEOWFFICER') == [2]


def test_meow_check_requires_prior_confirmation():
    """召回弹窗从第一帧就压住标题条时：confirmed 一直是 False，
    具名检查不会被调用（AP 原样行为，也是已接受的取舍）。"""
    stub = ConfirmLoopStub()
    assert stub.strategic_search_confirm() is None
    assert popup_frames(stub, 'STRATEGIC_SEARCH') == []
    assert popup_frames(stub, 'STRATEGIC_SEARCH_MEOWFFICER') == []


def test_recall_popup_gets_confirmed_then_the_round_continues():
    """召回弹窗在第 2 帧被具名检查点掉；第 3 帧检查落空后回地图正常返回。"""
    stub = ConfirmLoopStub(title_bar_ok_frames={1}, meow_ok_frames={2}, in_map_from=3)
    assert stub.strategic_search_confirm() is True
    # confirmed 之后每一帧都会尝试召回检查，落空的那次也不跳过
    assert popup_frames(stub, 'STRATEGIC_SEARCH_MEOWFFICER') == [2, 3]
    assert [c[1] for c in stub.calls if c[0] == 'in_map'][-1] == 3


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

    # 通用确定键现在先问一次「暂时离开大型作战」守卫；这里没那个弹窗，守卫
    handle_leave_os_popup = InfoHandler.handle_leave_os_popup
    handle_popup_confirm = InfoHandler.handle_popup_confirm


def test_default_threshold_still_misses_the_darkened_recall_popup():
    """默认阈值 10 下取消键差 14 就认不出 -> 什么都不会点（不是盲点）。

    这条保留为已知取舍的记录：AP 原样结构在偏暗召回弹窗上可能点不动，
    历史提交 f3d6fe822 的放宽阈值方案是备选回滚方案。
    """
    stub = PopupStub()
    assert stub.handle_popup_confirm(name='STRATEGIC_SEARCH') is False
    assert stub.clicks == []


def test_loosened_threshold_confirms_without_blind_clicking():
    """InfoHandler 的 threshold 参数保留着：实机若复现点不动，调用点可放宽。"""
    stub = PopupStub()
    assert stub.handle_popup_confirm(name='STRATEGIC_SEARCH', threshold=20) is True
    assert stub.clicks == ['POPUP_CONFIRM_STRATEGIC_SEARCH']


def test_button_name_is_restored_after_the_click():
    """临时改名必须还原，否则后续所有弹窗日志都会带上前一次的标记。"""
    stub = PopupStub()
    stub.handle_popup_confirm(name='STRATEGIC_SEARCH', threshold=20)
    stub.handle_popup_confirm(name='GOTO_GLOBE', threshold=20)
    assert stub.clicks == ['POPUP_CONFIRM_STRATEGIC_SEARCH', 'POPUP_CONFIRM_GOTO_GLOBE']


class StartStub:
    """替身只记录调用顺序，用来看「跳过计划作战滑动检查」到底跳掉了哪一步。"""

    strategic_search_start = StrategicSearchHandler.strategic_search_start

    def __init__(self, skip_check, option_ok=True):
        self.config = SimpleNamespace(OpsiGeneral_SkipStrategicSearchCheck=skip_check)
        self.option_ok = option_ok
        self.calls = []

    def strategy_search_enter(self):
        self.calls.append('enter')

    def strategic_search_set_tab(self):
        self.calls.append('tab')

    def strategic_search_set_option(self):
        self.calls.append('option')
        return self.option_ok

    def strategic_search_confirm(self):
        self.calls.append('confirm')


def test_option_check_runs_when_switch_is_off():
    stub = StartStub(skip_check=False)
    assert stub.strategic_search_start() is True
    assert stub.calls == ['enter', 'tab', 'option', 'confirm']


def test_quick_mode_skips_the_scroll_check_entirely():
    stub = StartStub(skip_check=True)
    assert stub.strategic_search_start() is True
    assert stub.calls == ['enter', 'tab', 'confirm']


def test_failed_option_check_still_retries_only_while_checking():
    """关掉快速模式时，选项检查失败仍按原逻辑重试三次后判启动失败。"""
    stub = StartStub(skip_check=False, option_ok=False)
    assert stub.strategic_search_start() is False
    assert stub.calls == ['enter', 'tab', 'option'] * 3
