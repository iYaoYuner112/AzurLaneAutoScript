"""大世界每日「进不去的委托海域」逐条延期（对齐 AzurPilot 5979fc1cb）。

AzurPilot 把延期记录写进 `OpsiDaily.OpsiDaily.DeferredMissions` 配置项，我们刻意不新增
配置项，改成同一进程内的内存记录：记录只需要活过本轮，下一轮重试一次正是手动重跑
会做的事。这里把新加的四个部分都钉住：

1. `no_meowfficer_searching()`：保留海域的中断判定要多看一个「奖励弹窗已消失」；
2. `daily_interrupt_check()`：换成 1 之后，弹窗还在时不能中断自律寻敌；
3. `_os_defer_mission_zone()` / `_os_deferred_mission_zones()`：内存记录，逐实例隔离；
4. `_os_find_checkout_offset_skip_monthly_boss()`：跳过月度 Boss 行找下一个委托；
   `skip>0` 时页面装不下要往下滚。
"""

from time import sleep
from types import SimpleNamespace

import numpy as np

from module.os.map_operation import OSMapOperation
from module.os.tasks.daily import OpsiDaily
from module.os_handler.assets import MISSION_CHECKOUT, MISSION_MONTHLY_BOSS
from module.os_handler.mission import MissionHandler

# 委托行间距，和 `_os_find_checkout_offset_skip_monthly_boss()` 里的 110 一致
ROW = 110


# ---- 1. no_meowfficer_searching ----


class SearchStateStub:
    no_meowfficer_searching = OSMapOperation.no_meowfficer_searching

    def __init__(self, meow=False, reward=False):
        self.meow = meow
        self.reward = reward

    def appear(self, button, offset=0, **kwargs):
        return self.reward and button is not None

    def is_meowfficer_searching(self):
        return self.meow


def test_no_meowfficer_searching_needs_both_signals_clear():
    assert SearchStateStub().no_meowfficer_searching() is True
    assert SearchStateStub(meow=True).no_meowfficer_searching() is False
    # 奖励弹窗还在时不算"搜索结束"，否则自律寻敌会在领奖前被切断
    assert SearchStateStub(reward=True).no_meowfficer_searching() is False
    assert SearchStateStub(meow=True, reward=True).no_meowfficer_searching() is False


# ---- 2. daily_interrupt_check ----


class InterruptStub:
    daily_interrupt_check = OpsiDaily.daily_interrupt_check
    no_meowfficer_searching = OSMapOperation.no_meowfficer_searching

    def __init__(self, complete=False, meow=False, reward=False):
        self.complete = complete
        self.meow = meow
        self.reward = reward
        self._os_mission_complete = False

    def _os_daily_mission_complete_check(self):
        return self.complete

    def appear(self, button, offset=0, **kwargs):
        return self.reward

    def is_meowfficer_searching(self):
        return self.meow


def test_interrupt_waits_for_the_reward_popup():
    """AP 的行为：委托完成但领奖弹窗还挂着时不中断。"""
    stub = InterruptStub(complete=True, reward=True)
    assert stub.daily_interrupt_check() is False
    assert stub._os_mission_complete is True
    stub.reward = False
    assert stub.daily_interrupt_check() is True


def test_interrupt_waits_for_the_meowfficer_search():
    stub = InterruptStub(complete=True, meow=True)
    assert stub.daily_interrupt_check() is False
    stub.meow = False
    assert stub.daily_interrupt_check() is True


def test_interrupt_not_before_the_mission_is_complete():
    assert InterruptStub(complete=False, meow=False, reward=False).daily_interrupt_check() is False


# ---- 3. 延期记录：内存、逐实例 ----


class DeferStub:
    _os_deferred_zones = None
    _os_deferred_mission_zones = MissionHandler._os_deferred_mission_zones
    _os_defer_mission_zone = MissionHandler._os_defer_mission_zone


def test_deferred_zones_accumulate_in_memory():
    stub = DeferStub()
    assert stub._os_deferred_mission_zones() == set()
    stub._os_defer_mission_zone(SimpleNamespace(zone_id=10))
    stub._os_defer_mission_zone(SimpleNamespace(zone_id=44))
    assert stub._os_deferred_mission_zones() == {10, 44}


def test_deferred_zones_are_not_shared_between_instances():
    """记录必须挂在实例上：类属性被共享的话，第二个实例会白跳过一片海域。"""
    first, second = DeferStub(), DeferStub()
    first._os_defer_mission_zone(SimpleNamespace(zone_id=10))
    assert second._os_deferred_mission_zones() == set()
    assert DeferStub._os_deferred_zones is None


# ---- 4a. 跳过月度 Boss 找下一个委托（skip=0）----


def row_offset(index):
    """第 index 个委托行的点击偏移，和 `_os_mission_checkout_offsets()` 同口径。"""
    return (-20, -20 + ROW * index, 20, 20 + ROW * index)


class CheckoutStub:
    _os_find_checkout_offset_skip_monthly_boss = MissionHandler._os_find_checkout_offset_skip_monthly_boss

    def __init__(self, checkout_rows=(), boss_rows=()):
        self.checkout_rows = set(checkout_rows)
        self.boss_rows = set(boss_rows)
        self.probes = []

    @staticmethod
    def _row(offset):
        return (offset[1] + 20) // ROW

    def match_template_color(self, button, offset=0, similarity=0.85):
        row = self._row(offset)
        self.probes.append(row)
        return row in self.checkout_rows

    def appear(self, button, offset=0, **kwargs):
        return self._row(offset) in self.boss_rows


def test_first_checkout_row_wins_when_skip_is_zero():
    stub = CheckoutStub(checkout_rows={0})
    assert stub._os_find_checkout_offset_skip_monthly_boss(row_offset(0)) == row_offset(0)
    assert stub.probes == [0]


def test_monthly_boss_row_is_stepped_over():
    """月度 Boss 行上也有结算按钮，旧写法只是硬往下挪一行就点，这里按行扫。"""
    stub = CheckoutStub(checkout_rows={0, 1}, boss_rows={0})
    assert stub._os_find_checkout_offset_skip_monthly_boss(row_offset(0)) == row_offset(1)
    assert stub.probes == [0, 1]


def test_no_mission_row_returns_none():
    stub = CheckoutStub(checkout_rows=set(), boss_rows=set())
    assert stub._os_find_checkout_offset_skip_monthly_boss(row_offset(0)) is None
    assert len(stub.probes) == 8


def test_rows_that_are_all_monthly_boss_return_none():
    stub = CheckoutStub(checkout_rows={0, 1, 2}, boss_rows={0, 1, 2})
    assert stub._os_find_checkout_offset_skip_monthly_boss(row_offset(0)) is None


# ---- 4b. 跳过延期委托：一页装不下时要往下滚 ----


class ScrollStub:
    """只读 skip>0 那条滚动路径。

    `image_crop((600, 170, 1000, 650))` 是被搜索的画面，锚点是开拖之前截下来的那一行。
    拖完之后画面里的锚点整体上移 `scroll_distance`，`matchTemplate` 就能算出滚了多少。
    """

    _os_find_checkout_offset_skip_monthly_boss = MissionHandler._os_find_checkout_offset_skip_monthly_boss
    SEARCH_AREA = (600, 170, 1000, 650)

    def __init__(self, pages, skip, scroll_distance):
        self.pages = [list(page) for page in pages]
        self.skip = skip
        self.scroll_distance = scroll_distance
        self.drags = 0
        # 锚点高度 = anchor_y 到 y + 45，由第一页最后一个委托行决定
        first_last = self.pages[0][-1]
        y = MISSION_CHECKOUT.area[1] + first_last[1] + 20
        self.anchor_y = max(170, y - 35)
        self.pattern = (np.arange(80 * 400).reshape(80, 400) % 251).astype(np.uint8)
        self.pattern_top = self.anchor_y

    def loop(self, *args, **kwargs):
        for _ in range(200):
            sleep(0.05)  # Timer(0.3, count=2) 要求真的过掉 0.3 秒
            yield

    def _os_mission_checkout_offsets(self):
        return self.pages.pop(0) if self.pages else []

    def image_crop(self, area, copy=True):
        x1, y1, x2, y2 = area
        if (x1, y1, x2, y2) == self.SEARCH_AREA:
            image = np.zeros((y2 - y1, x2 - x1), dtype=np.uint8)
            top = self.pattern_top - y1
            if 0 <= top <= image.shape[0] - self.pattern.shape[0]:
                image[top:top + self.pattern.shape[0], :] = self.pattern
            return image
        return self.pattern.copy()

    def appear(self, button, offset=0, **kwargs):
        return False

    @property
    def device(self):
        return self

    def drag(self, p1, p2, **kwargs):
        self.drags += 1
        self.pattern_top -= self.scroll_distance


def test_a_page_that_cannot_hold_the_skipped_rows_is_scrolled():
    """第一页只有 4 个委托、要跳过 4 个：滚一屏把跳过的行扣掉，再从下一页取。"""
    stub = ScrollStub(
        pages=[[row_offset(0), row_offset(1), row_offset(2), row_offset(3)],
               [row_offset(0), row_offset(1)]],
        skip=4,
        scroll_distance=320,
    )
    assert stub._os_find_checkout_offset_skip_monthly_boss(row_offset(0), skip=stub.skip) == row_offset(1)
    assert stub.drags == 1


def test_scroll_that_reaches_the_bottom_gives_up():
    """拖了但列表没动（已经到底）：返回 None，别死等。"""
    stub = ScrollStub(pages=[[row_offset(0)]], skip=1, scroll_distance=0)
    assert stub._os_find_checkout_offset_skip_monthly_boss(row_offset(0), skip=stub.skip) is None
    assert stub.drags == 1


def test_skip_within_the_first_page_does_not_scroll():
    stub = ScrollStub(pages=[[row_offset(0), row_offset(1)]], skip=1, scroll_distance=320)
    assert stub._os_find_checkout_offset_skip_monthly_boss(row_offset(0), skip=stub.skip) == row_offset(1)
    assert stub.drags == 0
