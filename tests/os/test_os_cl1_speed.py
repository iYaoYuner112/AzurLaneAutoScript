"""侵蚀 1 / 短猫刷图提速的离线回归（对齐 AzurPilot dev 第一批）。

覆盖：
- `InfoHandler.story_skip(click_interval, prefer_skip)` 的快速路径与旧路径等价性；
- `MapEventHandler.story_skip()` 覆写为 1.2 秒（云环境保守档）并强制 prefer_skip；
- `handle_os_auto_search_map_option()` 对侵蚀 1 / 短猫使用 1.0 秒点击间隔，
  其它任务保持 3 秒；
- `_os_auto_search_enable_click()` 的 45 秒预算：预算内连续重试不被防连点
  机制误杀，超时抛 GameTooManyClickError，关闭外观消失时预算清零。
"""

import collections
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from module.base.button import Button
from module.base.timer import Timer
from module.exception import CampaignEnd, GameTooManyClickError
from module.handler.assets import STORY_CLOSE, STORY_SKIP_3
from module.handler.info_handler import InfoHandler
from module.os_handler.assets import (
    AUTO_SEARCH_OS_MAP_OPTION_OFF,
    AUTO_SEARCH_REWARD,
)
from module.os_handler.map_event import MapEventHandler

OPTION_OFF_NAME = AUTO_SEARCH_OS_MAP_OPTION_OFF.name


def make_option(name='STORY_OPTION_2_OF_3'):
    """构造一个与 _story_option_buttons_2 同名的剧情选项按钮。"""
    return Button(area=(330, 200, 980, 250), color=(247, 247, 247),
                  button=(330, 200, 980, 250), name=name)


class VirtualClock:
    """可控时钟：patch 掉 module.base.timer.time。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeDevice:
    """复刻 device 的点击记录与防连点规则。

    最近 15 次点击中，同一个按钮 >=12 次，或两个按钮各 >=6 次即判定为卡死。
    只有 `click_record_clear()` 能打断累计——这正是自律开启重试依赖的机制。
    """

    def __init__(self):
        self.click_record = collections.deque(maxlen=15)
        self.history = []
        self.clears = 0
        self.image = None

    @staticmethod
    def _name(button):
        return button.name if hasattr(button, 'name') else str(button)

    def click(self, button, *args, **kwargs):
        name = self._name(button)
        self.history.append(name)
        self.click_record.append(name)
        self._check()

    def click_record_clear(self):
        self.clears += 1
        self.click_record.clear()

    def stuck_record_add(self, button):
        pass

    def screenshot(self):
        pass

    def screenshot_interval_set(self):
        pass

    def count_of(self, name):
        return sum(1 for clicked in self.history if clicked == name)

    def _check(self):
        counter = collections.Counter(self.click_record).most_common(2)
        if counter and counter[0][1] >= 12:
            raise GameTooManyClickError(f'[设备-点击] 按钮点击次数过多: {counter[0][0]}')
        if len(counter) >= 2 and counter[0][1] >= 6 and counter[1][1] >= 6:
            raise GameTooManyClickError(
                f'[设备-点击] 两个按钮交替点击次数过多: {counter[0][0]}, {counter[1][0]}')


class MapEventStub(MapEventHandler):
    """保留真实的 appear / interval_* 逻辑，只替换设备与画面识别。"""

    ensure_button = staticmethod(lambda button: button)

    def __init__(self, command='OpsiHazard1Leveling'):
        self.config = SimpleNamespace(
            task=SimpleNamespace(command=command),
            STORY_ALLOW_SKIP=False,
            STORY_OPTION=0,
        )
        self.device = FakeDevice()
        self.interval_timer = {}
        self.story_present = False
        self.option_buttons = []
        self.template_result = {}
        self.match_calls = []
        self.story_popup_timeout = Timer(10, count=20)
        self._story_confirm = Timer(0.5, count=1)
        self._story_option_timer = Timer(2)
        self._story_option_record = 0
        self._story_option_confirm = Timer(0.3, count=0)
        # 真实实现会去匹配弹窗图像，测试里统一当作没有弹窗
        self.handle_popup_confirm = lambda *args, **kwargs: False

    def _is_story_black(self):
        return False

    def _story_option_buttons_2(self):
        return list(self.option_buttons)

    def match_template_color(self, button, offset=0, interval=0, similarity=0.85, threshold=10):
        self.match_calls.append((button.name, interval))
        return bool(self.template_result.get(button.name, False))

    def is_in_map(self):
        return True

    def info_bar_count(self):
        return 0


class SpeedTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        self.time_patch = patch('module.base.timer.time', self.clock)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)


# ---- 剧情快速跳过 ----

class TestFastStorySkip(SpeedTestBase):
    def test_fast_path_clicks_skip_without_global_config(self):
        """大世界覆写：0.5 秒间隔 + prefer_skip，STORY_ALLOW_SKIP=False 也点右上角跳过。"""
        handler = MapEventStub()
        self.assertFalse(handler.config.STORY_ALLOW_SKIP)
        with patch.object(STORY_SKIP_3, 'match', return_value=True), \
                patch.object(STORY_CLOSE, 'match', return_value=False):
            self.assertFalse(handler.story_skip())
            self.clock.advance(0.35)
            self.assertTrue(handler.story_skip())
        self.assertEqual(handler.device.count_of('STORY_SKIP'), 1)
        self.assertEqual(handler.device.count_of('CLICK_SAFE_AREA'), 0)
        # 覆写把选项计时器换成实例级的保守档计时器（云环境 1.2 秒）
        self.assertEqual(handler._story_option_timer.limit, 1.2)

    def test_default_interval_keeps_legacy_behaviour(self):
        """基类默认参数（click_interval=2）行为不变：走原分支点空白区。"""
        handler = MapEventStub()
        handler._story_confirm = Timer(0.5, count=1).start()
        with patch.object(STORY_SKIP_3, 'match', return_value=True), \
                patch.object(STORY_CLOSE, 'match', return_value=False):
            # 直接调基类实现，绕开 MapEventHandler 的 0.5 秒覆写
            InfoHandler.story_skip(handler, click_interval=2)
            self.clock.advance(2.5)
            self.assertTrue(InfoHandler.story_skip(handler, click_interval=2))
        self.assertEqual(handler.device.count_of('CLICK_SAFE_AREA'), 1)
        self.assertEqual(handler.device.count_of('STORY_SKIP'), 0)

    def test_fast_path_does_not_blank_click_while_options_are_confirming(self):
        """快路径下选项还在稳定确认时，不能去点空白区把剧情点掉。"""
        handler = MapEventStub()
        handler.option_buttons = [make_option('STORY_OPTION_1_OF_3')]
        with patch.object(STORY_SKIP_3, 'match', return_value=True), \
                patch.object(STORY_CLOSE, 'match', return_value=False):
            # 选项数量第一次识别（record 从 0 变为 1）→ 只记录，不点任何东西
            self.assertFalse(handler.story_skip())
        self.assertEqual(handler.device.count_of('STORY_SKIP'), 0)
        self.assertEqual(handler.device.count_of('CLICK_SAFE_AREA'), 0)

    def _drive_option_clicks(self, handler, clicks):
        """驱动「识别 → 确认 → 点击选项」循环，每次点击占两次 story_skip 调用。"""
        with patch.object(STORY_SKIP_3, 'match', return_value=True), \
                patch.object(STORY_CLOSE, 'match', return_value=False):
            for _ in range(clicks * 2 + 1):
                # 步长要大于保守档的选项冷却（1.2 秒）
                self.clock.advance(1.5)
                handler.story_skip()

    def test_option_clicks_survive_the_click_guard(self):
        """同名选项连续点击（塞壬装置场景）不再被防连点机制误杀。

        选项按钮名按「第几个/共几个」生成，多组剧情段共用同一个名字；旧实现下
        第 12 次就报 GameTooManyClickError，清空点击记录后改由计数器兜底。
        """
        handler = MapEventStub()
        handler._story_option_click_limit = 30  # 放大上限，只看防连点是否被绕过
        handler.option_buttons = [make_option()]
        self._drive_option_clicks(handler, 20)
        self.assertEqual(handler.device.clears, 20)
        self.assertEqual(handler._story_option_click, 20)

    def test_stuck_story_options_still_raise(self):
        """剧情真的卡在选项画面时仍要报错，不能因为清记录而失去保护。"""
        handler = MapEventStub()
        handler.option_buttons = [make_option()]
        with self.assertRaises(GameTooManyClickError):
            self._drive_option_clicks(handler, handler._story_option_click_limit)


# ---- 自律寻敌开启：0.5 秒重试 + 45 秒预算 ----

class TestAutoSearchEnableRetry(SpeedTestBase):
    def _run_enable(self, handler):
        with patch.object(STORY_SKIP_3, 'match', return_value=False), \
                patch.object(AUTO_SEARCH_REWARD, 'match', return_value=False):
            return handler.handle_os_auto_search_map_option()

    def test_fast_farming_uses_one_second_interval(self):
        handler = MapEventStub(command='OpsiHazard1Leveling')
        handler.template_result[OPTION_OFF_NAME] = True
        self.assertTrue(self._run_enable(handler))
        self.assertIn((OPTION_OFF_NAME, 1.0), handler.match_calls)
        self.assertEqual(handler.device.count_of(OPTION_OFF_NAME), 1)

    def test_meowfficer_farming_is_also_fast(self):
        handler = MapEventStub(command='OpsiMeowfficerFarming')
        handler.template_result[OPTION_OFF_NAME] = True
        self._run_enable(handler)
        self.assertIn((OPTION_OFF_NAME, 1.0), handler.match_calls)

    def test_other_tasks_keep_three_second_interval(self):
        handler = MapEventStub(command='OpsiDaily')
        handler.template_result[OPTION_OFF_NAME] = True
        self._run_enable(handler)
        self.assertIn((OPTION_OFF_NAME, 3), handler.match_calls)

    def test_retry_within_budget_survives_the_click_guard(self):
        """0.5 秒间隔连续重试 20 次：旧实现会在第 12 次被防连点误杀。"""
        handler = MapEventStub()
        for _ in range(20):
            handler._os_auto_search_enable_click(AUTO_SEARCH_OS_MAP_OPTION_OFF)
            self.clock.advance(0.5)
        self.assertEqual(handler.device.count_of(OPTION_OFF_NAME), 20)
        self.assertEqual(handler.device.clears, 20)

    def test_retry_beyond_budget_raises(self):
        handler = MapEventStub()
        handler._os_auto_search_enable_click(AUTO_SEARCH_OS_MAP_OPTION_OFF)
        self.clock.advance(46)
        with self.assertRaises(GameTooManyClickError):
            handler._os_auto_search_enable_click(AUTO_SEARCH_OS_MAP_OPTION_OFF)

    def test_budget_clear_restarts_the_window(self):
        handler = MapEventStub()
        handler._os_auto_search_enable_click(AUTO_SEARCH_OS_MAP_OPTION_OFF)
        self.clock.advance(46)
        handler._os_auto_search_enable_budget_clear()
        handler._os_auto_search_enable_click(AUTO_SEARCH_OS_MAP_OPTION_OFF)
        self.assertEqual(handler.device.count_of(OPTION_OFF_NAME), 2)

    def test_story_on_screen_defers_enable_without_consuming_budget(self):
        """剧情挡住地图按钮时先返回处理剧情，且不开重试预算。"""
        handler = MapEventStub()
        handler.template_result[OPTION_OFF_NAME] = True
        with patch.object(STORY_SKIP_3, 'match', return_value=True), \
                patch.object(AUTO_SEARCH_REWARD, 'match', return_value=False):
            self.assertFalse(handler.handle_os_auto_search_map_option())
        self.assertNotIn('_os_auto_search_enable_timer', handler.__dict__)
        self.assertEqual(handler.device.count_of(OPTION_OFF_NAME), 0)


# ---- 第二批：奖励确认后直接收尾，不再空跑一次自律 ----

class TestAutoSearchRewardFinish(SpeedTestBase):
    def _reward_appears(self, handler):
        """让 AUTO_SEARCH_REWARD 出现，并让 os_auto_search_quit 报告「海域未清空」。"""
        handler.os_auto_search_quit = lambda drop=None: False
        return patch.object(AUTO_SEARCH_REWARD, 'match', return_value=True)

    def test_confirmed_start_finishes_search(self):
        """本轮已确认开启过自律 → 奖励出现即结束本次搜索。"""
        handler = MapEventStub(command='OpsiHazard1Leveling')
        handler._os_auto_search_started = True
        with self._reward_appears(handler), \
                patch.object(STORY_SKIP_3, 'match', return_value=False):
            with self.assertRaises(CampaignEnd):
                handler.handle_os_auto_search_map_option()

    def test_unconfirmed_reward_keeps_the_old_recovery(self):
        """没确认开启过（可能是上个海域延迟弹的奖励）→ 保留原来的恢复路径。"""
        handler = MapEventStub(command='OpsiHazard1Leveling')
        handler._os_auto_search_started = False
        with self._reward_appears(handler), \
                patch.object(STORY_SKIP_3, 'match', return_value=False):
            self.assertTrue(handler.handle_os_auto_search_map_option())

    def test_other_task_never_finishes_on_reward(self):
        """非刷图任务（如大世界每日）不吃这个优化。"""
        handler = MapEventStub(command='OpsiDaily')
        handler._os_auto_search_started = True
        with self._reward_appears(handler), \
                patch.object(STORY_SKIP_3, 'match', return_value=False):
            self.assertTrue(handler.handle_os_auto_search_map_option())

    def test_meowfficer_farming_also_finishes(self):
        handler = MapEventStub(command='OpsiMeowfficerFarming')
        handler._os_auto_search_started = True
        with self._reward_appears(handler), \
                patch.object(STORY_SKIP_3, 'match', return_value=False):
            with self.assertRaises(CampaignEnd):
                handler.handle_os_auto_search_map_option()


if __name__ == '__main__':
    unittest.main()
