"""战略搜索里的任务切换让位（对齐 AzurPilot）。

AP 4e8a2f103 / 72b4ac8ac（2026-07-05）后，短猫（OpsiMeowfficerFarming）的
战略搜索遇到待切换任务时会先跑完本次搜索，由搜索结束后的
`check_task_switch()` 收尾；停止事件仍然立即中断，禁用了任务切换的一轮
代理任务也继续搜索。
"""

from types import SimpleNamespace

from module.os.map import OSMap


class StopEvent:
    def __init__(self, value=False):
        self.value = value

    def is_set(self):
        return self.value


def make_stub(command, switched, stop=False, disabled=False):
    return SimpleNamespace(
        config=SimpleNamespace(
            stop_event=StopEvent(stop),
            _disable_task_switch=disabled,
            task=SimpleNamespace(command=command),
            task_switched=lambda: switched,
        )
    )


def test_short_meow_strategic_search_defers_task_switch():
    stub = make_stub('OpsiMeowfficerFarming', switched=True)
    assert OSMap._os_auto_search_should_interrupt(stub, True) is False


def test_short_meow_strategic_search_still_stops_on_stop_event():
    stub = make_stub('OpsiMeowfficerFarming', switched=True, stop=True)
    assert OSMap._os_auto_search_should_interrupt(stub, True) is True


def test_other_task_strategic_search_interrupts():
    stub = make_stub('OpsiHazard1Leveling', switched=True)
    assert OSMap._os_auto_search_should_interrupt(stub, True) is True


def test_no_pending_switch_keeps_searching():
    stub = make_stub('OpsiHazard1Leveling', switched=False)
    assert OSMap._os_auto_search_should_interrupt(stub, True) is False


def test_disabled_task_switch_keeps_searching():
    stub = make_stub('OpsiStronghold', switched=True, disabled=True)
    assert OSMap._os_auto_search_should_interrupt(stub, True) is False


def test_non_strategic_search_is_not_interrupted():
    stub = make_stub('OpsiMeowfficerFarming', switched=True, stop=True)
    assert OSMap._os_auto_search_should_interrupt(stub, False) is False
