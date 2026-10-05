"""「需要暂时离开大型作战么?」弹窗：点它自己的 X，绝不能用通用确定键。

这个弹窗是快速点击时误点到海域地图外弹出来的，**确定按钮和 POPUP_CONFIRM 同一个位置**，
按通用弹窗处理就会直接退出大型作战；不理它则一直卡到设备卡死检测重启游戏（AzurPilot
e71945363 附的 2026-09-28 现场日志就是这两个后果）。所以判定顺序必须是：先认「暂时离开」
文字 → 点右上角 X → 当作"弹窗已处理"返回 True，拦住后面所有确定分支。

这里锁定短路顺序、间隔保护期间仍然算"弹窗在"、其它弹窗不受影响，以及退出港口的回调确实
接到了这个处理函数。
"""

from types import SimpleNamespace

from module.handler.assets import POPUP_CANCEL, POPUP_CONFIRM
from module.handler.info_handler import InfoHandler
from module.os_handler.assets import LEAVE_OS_POPUP_CHECK, LEAVE_OS_POPUP_CLOSE
from module.os_handler.port import PortHandler
from module.ui.assets import BACK_ARROW
from module.ui.ui import UI
from module.ui_white.assets import POPUP_CONFIRM_WHITE


class PopupStub:
    """`InfoHandler` 的弹窗部分：按"屏幕上有哪些按钮"来回答 appear 查询。"""

    _popup_offset = (20, 20)
    handle_leave_os_popup = InfoHandler.handle_leave_os_popup
    handle_popup_confirm = InfoHandler.handle_popup_confirm

    def __init__(self, on_screen=(), close_clickable=True):
        self.on_screen = [getattr(b, 'name', str(b)) for b in on_screen]
        self.close_clickable = close_clickable
        self.clicks = []
        self.appear_calls = []
        self.device = SimpleNamespace(click=self._click)

    def _click(self, button):
        self.clicks.append(button.name)

    def appear(self, button, offset=None, interval=None, threshold=None, similarity=None, **kwargs):
        self.appear_calls.append(button.name)
        return button.name in self.on_screen

    def appear_then_click(self, button, offset=None, interval=None, **kwargs):
        # 真实现在 interval 冷却期内返回 False（这一帧不点），但弹窗仍然在屏幕上
        if button.name not in self.on_screen:
            return False
        if not self.close_clickable:
            return False
        self._click(button)
        return True


def leave_os(*others, close_clickable=True):
    return PopupStub(
        [LEAVE_OS_POPUP_CHECK, LEAVE_OS_POPUP_CLOSE] + list(others), close_clickable=close_clickable)


def test_leave_os_popup_is_closed_instead_of_confirmed():
    stub = leave_os(POPUP_CANCEL, POPUP_CONFIRM)
    assert stub.handle_popup_confirm(name='GOTO_GLOBE') is True
    assert stub.clicks == ['LEAVE_OS_POPUP_CLOSE'], stub.clicks
    assert 'POPUP_CONFIRM' not in stub.clicks


def test_check_short_circuits_the_confirm_buttons():
    """认到「暂时离开」之后就不该再去匹配取消/确定键。"""
    stub = leave_os(POPUP_CANCEL, POPUP_CONFIRM)
    stub.handle_popup_confirm(name='GOTO_GLOBE')
    assert stub.appear_calls == ['LEAVE_OS_POPUP_CHECK'], stub.appear_calls


def test_white_confirm_is_blocked_as_well():
    stub = leave_os(POPUP_CONFIRM_WHITE)
    assert stub.handle_popup_confirm(name='OPSI') is True
    assert stub.clicks == ['LEAVE_OS_POPUP_CLOSE'], stub.clicks
    assert 'POPUP_CONFIRM_WHITE' not in stub.clicks


def test_popup_stays_handled_while_the_close_is_on_interval():
    """关闭键有 2 秒间隔保护：冷却帧里不点，但仍要报告"弹窗在"，调用方才会继续等。"""
    stub = leave_os(POPUP_CANCEL, POPUP_CONFIRM, close_clickable=False)
    assert stub.handle_popup_confirm(name='GOTO_GLOBE') is True
    assert stub.clicks == []


def test_no_leave_os_popup_leaves_the_old_behaviour():
    stub = PopupStub([POPUP_CANCEL, POPUP_CONFIRM])
    assert stub.handle_popup_confirm(name='GOTO_GLOBE') is True
    # 点的时候日志后缀还在身上，这正是"确认了哪个调用点"的记法
    assert stub.clicks == ['POPUP_CONFIRM_GOTO_GLOBE'], stub.clicks
    # 后缀必须还原干净，别留在全局按钮上
    assert POPUP_CONFIRM.name == 'POPUP_CONFIRM', POPUP_CONFIRM.name


def test_empty_screen_confirms_nothing():
    stub = PopupStub()
    assert stub.handle_popup_confirm(name='GOTO_GLOBE') is False
    assert stub.clicks == []


class QuitStub:
    """`port_quit` 需要的东西：接住 ui_back 的参数。"""

    handle_leave_os_popup = InfoHandler.handle_leave_os_popup

    def __init__(self):
        self.back_kwargs = None

    def ui_back(self, **kwargs):
        self.back_kwargs = kwargs

    def is_in_map(self):
        return True

    def wait_os_map_buttons(self):
        pass


def test_port_quit_passes_the_guard_to_ui_back():
    stub = QuitStub()
    PortHandler.port_quit(stub)
    assert stub.back_kwargs['additional'] == stub.handle_leave_os_popup


def test_ui_back_forwards_additional_to_ui_click():
    captured = {}

    class ClickStub:
        ui_click = UI.ui_click

        def device_click(self, button):
            pass

    stub = ClickStub()
    stub.ui_click = lambda **kwargs: captured.update(kwargs)
    callback = lambda: False
    UI.ui_back(stub, check_button=lambda: True, appear_button=BACK_ARROW, additional=callback)
    assert captured['additional'] is callback
