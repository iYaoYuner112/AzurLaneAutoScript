from module.base.utils import get_color
from module.logger import logger
from module.os_handler.assets import *
from module.os_handler.map_event import MapEventHandler
from module.ui.scroll import Scroll

STRATEGIC_SEARCH_SCROLL = Scroll(STRATEGIC_SEARCH_SCROLL_AREA, color=(247, 211, 66), name='STRATEGIC_SEARCH_SCROLL')


class StrategicSearchHandler(MapEventHandler):
    def strategy_search_enter(self):
        logger.info('Strategic search enter')
        self.interval_clear(STRATEGIC_SEARCH_MAP_OPTION_OFF)
        for _ in self.loop():
            # End
            if self.appear(STRATEGIC_SEARCH_POPUP_CHECK, offset=(20, 20)):
                return True

            if self.handle_map_event():
                continue
            if self.appear(AUTO_SEARCH_REWARD, offset=(50, 50)):
                continue
            if self.match_template_color(STRATEGIC_SEARCH_MAP_OPTION_OFF, offset=(20, 20), interval=2):
                self.device.click(STRATEGIC_SEARCH_MAP_OPTION_OFF)
                continue

    def strategic_search_set_tab(self):
        logger.info('Strategic search set tab')
        for _ in self.loop():
            if get_color(self.device.image, STRATEGIC_SEARCH_TAB_SECURED.area)[2] <= 150:
                self.device.click(STRATEGIC_SEARCH_TAB_SECURED)
                continue
            if get_color(self.device.image, STRATEGIC_SEARCH_TAB_SECURED.area)[2] > 150:
                break

    def _strategy_search_scroll_appear(self):
        """
        Returns:
            bool: If it still exists
        """
        for _ in self.loop(timeout=2):
            if STRATEGIC_SEARCH_SCROLL.appear(main=self):
                return True
            else:
                logger.warning('STRATEGIC_SEARCH_SCROLL disappeared')
        else:
            logger.warning('STRATEGIC_SEARCH_SCROLL disappeared confirm')
            return False

    def _strategy_option_selected(self, button):
        """
        Check if a button is selected
        """
        return self.image_color_count(button.button, color=(156, 255, 82), count=30)

    def strategic_search_set_option(self):
        """
        Returns:
            If success. False if strategic settings closed for unknown reason.
        """
        logger.info('Strategic search set option')
        for _ in self.loop():
            if self._strategy_option_selected(STRATEGIC_SEARCH_ZONEMODE_REPEAT) \
                    and self._strategy_option_selected(STRATEGIC_SEARCH_MERCHANT_STOP):
                logger.attr('zone_mode', 'repeat')
                logger.attr('encounter_merchant', 'stop')
                break
            if self._strategy_option_selected(STRATEGIC_SEARCH_ZONEMODE_RANDOM):
                logger.attr('zone_mode', 'random')
                self.device.click(STRATEGIC_SEARCH_ZONEMODE_REPEAT)
                continue
            if self._strategy_option_selected(STRATEGIC_SEARCH_MERCHANT_CONTINUE):
                logger.attr('encounter_merchant', 'continue')
                self.device.click(STRATEGIC_SEARCH_MERCHANT_STOP)
                continue

        STRATEGIC_SEARCH_SCROLL.drag_threshold = 0.1
        STRATEGIC_SEARCH_SCROLL.set(0.5, main=self)
        if not self._strategy_search_scroll_appear():
            return False

        for _ in self.loop():
            self.appear(STRATEGIC_SEARCH_DEVICE_CHECK, offset=(20, 200), similarity=0.7)
            STRATEGIC_SEARCH_DEVICE_STOP.load_offset(STRATEGIC_SEARCH_DEVICE_CHECK)
            STRATEGIC_SEARCH_DEVICE_CONTINUE.load_offset(STRATEGIC_SEARCH_DEVICE_CHECK)

            if self._strategy_option_selected(STRATEGIC_SEARCH_DEVICE_STOP):
                logger.attr('encounter_device', 'stop')
                break
            if self._strategy_option_selected(STRATEGIC_SEARCH_DEVICE_CONTINUE):
                logger.attr('encounter_device', 'continue')
                self.device.click(STRATEGIC_SEARCH_DEVICE_STOP)
                continue

        STRATEGIC_SEARCH_SCROLL.drag_threshold = 0.05
        STRATEGIC_SEARCH_SCROLL.edge_add = (0.5, 0.8)
        STRATEGIC_SEARCH_SCROLL.set_bottom(main=self)
        if not self._strategy_search_scroll_appear():
            return False

        for _ in self.loop():
            self.appear(STRATEGIC_SEARCH_SUBMIT_CHECK, offset=(20, 20), similarity=0.7)
            STRATEGIC_SEARCH_SUBMIT_OFF.load_offset(STRATEGIC_SEARCH_SUBMIT_CHECK)
            STRATEGIC_SEARCH_SUBMIT_ON.load_offset(STRATEGIC_SEARCH_SUBMIT_CHECK)

            if self._strategy_option_selected(STRATEGIC_SEARCH_SUBMIT_ON):
                logger.attr('auto_submit', 'on')
                break
            if self._strategy_option_selected(STRATEGIC_SEARCH_SUBMIT_OFF):
                logger.attr('auto_submit', 'off')
                self.device.click(STRATEGIC_SEARCH_SUBMIT_ON)
                continue

        return True

    def strategic_search_confirm(self):
        logger.info('Strategic search confirm')
        # 第一段（前 3 帧，约 1 秒）：只认计划作战确认自己的标题条，行为与原来完全一致。
        # 第二段：标题条一直认不出来，说明卡在了另一层弹窗上 —— 海域里有指挥喵在搜寻时，
        # 游戏会在确认之后追加一层「是否强制召回」的标准弹窗。它压在确认弹窗之上、整体偏暗，
        # 实测标题条均色 (97,135,179) vs 定义 (150,170,209)，容差 53 远超阈值 10，所以旧逻辑
        # 会一路空等到 60 秒被判卡死 -> 重启 -> 猫还在 -> 再撞，三次后停机等人工。
        # 这时退一步，按「取消 + 确定」两个标准按钮同时匹配来点掉它（不是按坐标盲点）。
        # 兜底只可能在前 3 帧严格判定失败之后生效，正常流程一步都不会多走。
        frames = 0
        for _ in self.loop():
            frames += 1
            if self.appear(STRATEGIC_SEARCH_POPUP_CHECK, offset=(20, 20)) \
                    and self.handle_popup_confirm(offset=(30, 30), name='STRATEGIC_SEARCH'):
                continue
            if self.is_in_map():
                return True
            if frames > 3 and self.handle_popup_confirm(
                    offset=(30, 30), name='STRATEGIC_SEARCH', threshold=20):
                logger.warning('Strategic search: an extra confirm popup covered the map, confirmed it')
                continue

    def strategic_search_start(self):
        """
        Returns:
            If success.

        Pages:
            in: IN_MAP
            out: IN_MAP, with strategic search running
        """
        logger.hr('Strategic search start')
        # 面板会保留上一次的选项配置：循环刷取同一海域时，每次重新滑动检查各选项
        # 属于重复动作，开启「跳过计划作战滑动检查」后直接确认开始。
        skip_check = self.config.OpsiGeneral_SkipStrategicSearchCheck
        if skip_check:
            logger.info('[大世界-策略] 已开启快速模式，跳过计划作战面板选项检查')
        for _ in range(3):
            self.strategy_search_enter()
            self.strategic_search_set_tab()
            if not skip_check:
                success = self.strategic_search_set_option()
                if not success:
                    continue
            self.strategic_search_confirm()
            return True

        logger.warning('Failed to start strategic search')
        return False
