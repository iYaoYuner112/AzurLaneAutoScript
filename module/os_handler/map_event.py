from module.base.timer import Timer
from module.combat.assets import *
from module.exception import CampaignEnd, GameTooManyClickError
from module.handler.assets import POPUP_CANCEL, POPUP_CONFIRM, STORY_SKIP_3
from module.logger import logger
from module.os.assets import GLOBE_GOTO_MAP
from module.os_handler.assets import *
from module.os_handler.enemy_searching import EnemySearchingHandler
from module.statistics.azurstats import DropImage
from module.ui.assets import BACK_ARROW
from module.ui.switch import Switch


class FleetLockSwitch(Switch):
    def handle_additional(self, main):
        # A game bug that AUTO_SEARCH_REWARD from the last cleared zone popups
        if main.appear_then_click(AUTO_SEARCH_REWARD, offset=(50, 50), interval=3):
            return True
        return False


fleet_lock = FleetLockSwitch('Fleet_Lock', offset=(10, 120))
fleet_lock.add_state('on', check_button=OS_FLEET_LOCKED)
fleet_lock.add_state('off', check_button=OS_FLEET_UNLOCKED)


class MapEventHandler(EnemySearchingHandler):
    ash_popup_canceled = False

    # 大世界剧情推进间隔（秒）。基类默认 2 秒是通用页面的保守值。
    # AP 上游用 0.5 秒；考虑云服务器 + 云手机的截图与指令延迟，这里取 1.2 秒：
    # 仍小于 2 秒，走同一条快速路径，但留给画面刷新的余量更大。
    # 想恢复 AP 原速直接改成 0.5；想退回旧行为改成 >= 2 即可。
    _os_story_click_interval = 1.2

    def story_skip(self, drop=None):
        """大世界按新截图快速点击右上角跳过，重要选项仍优先处理。

        `story_skip()` 的计时器在基类是类属性，这里改成实例级并压到 0.5 秒，
        避免改动其它页面共用的基类计时器。游戏的世界剧情跳过会停在重要选项上，
        快路径仍逐帧先处理选项；`prefer_skip=True` 让无选项对话直接点右上角
        STORY_SKIP，不依赖 os_init 设成 False 的全局 STORY_ALLOW_SKIP。
        """
        click_interval = self._os_story_click_interval
        if self.__dict__.get('_os_story_click_interval') != click_interval:
            self._os_story_click_interval = click_interval
            self._story_option_timer = Timer(click_interval)
            self._story_option_confirm = Timer(0.3).start()
            self._story_option_record = 0
            self._story_confirm = Timer(0.2, count=1).start()
        return super().story_skip(drop=drop, click_interval=click_interval, prefer_skip=True)

    def handle_map_get_items(self, interval=2, drop=None):
        if self.is_in_map():
            return False

        if self.appear(GET_ITEMS_1, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_ITEMS_1} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_ITEMS_2, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_ITEMS_2} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_ITEMS_3, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_ITEMS_3} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_ADAPTABILITY, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_ADAPTABILITY} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_MEOWFFICER_ITEMS_1, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_MEOWFFICER_ITEMS_1} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_MEOWFFICER_ITEMS_2, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_MEOWFFICER_ITEMS_2} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True

        return False

    def handle_map_archives(self, drop=None):
        if self.appear(MAP_ARCHIVES, interval=5):
            if drop:
                drop.add(self.device.image)
            logger.info(f'{MAP_ARCHIVES} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear_then_click(MAP_WORLD, offset=(20, 20), interval=5):
            return True

        return False

    def handle_os_game_tips(self):
        # Close game tips the first time enabling auto search
        if self.appear_then_click(OS_GAME_TIPS, offset=(20, 20), interval=3):
            return True

        return False

    def handle_ash_popup(self):
        name = 'ASH'
        # 2021.12.09
        # Ash popup no longer shows red letters, so change it to letter `Ashes Coordinates`
        if self.appear(POPUP_CONFIRM, offset=self._popup_offset) \
                and self.appear(POPUP_CANCEL, offset=self._popup_offset, interval=2) \
                and self.appear(ASH_POPUP_CHECK, offset=(20, 20)):
            POPUP_CANCEL.name = POPUP_CANCEL.name + '_' + name
            self.device.click(POPUP_CANCEL)
            POPUP_CANCEL.name = POPUP_CANCEL.name[:-len(name) - 1]
            self.ash_popup_canceled = True
            return True
        else:
            return False

    def handle_map_event(self, drop=None):
        """
        Args:
            drop (DropImage):

        Returns:
            str: Event that handled
        """
        if self.handle_map_get_items(drop=drop):
            return 'map_get_items'
        if self.handle_os_game_tips():
            return 'os_game_tips'
        if self.handle_map_archives(drop=drop):
            return 'map_archives'
        if self.handle_guild_popup_cancel():
            return 'guild_popup_cancel'
        if self.handle_ash_popup():
            return 'ash_popup'
        if self.handle_urgent_commission(drop=drop):
            return 'urgent_commission'
        if self.handle_story_skip():
            return 'story_skip'

        return ''

    _os_in_map_confirm_timer = Timer(1.5, count=3)

    def handle_os_in_map(self):
        """
        Returns:
            bool: If is in map and confirmed.
        """
        if self.is_in_map():
            if self._os_in_map_confirm_timer.reached():
                return True
            else:
                return False
        else:
            self._os_in_map_confirm_timer.reset()
            return False

    def ensure_no_map_event(self):
        self._os_in_map_confirm_timer.reset()

        for _ in self.loop():
            if self.handle_map_event():
                continue
            # End
            if self.handle_os_in_map():
                break

    def os_auto_search_quit(self, drop=None):
        """
        Args:
            drop (DropImage):

        Returns:
            bool: True if current map cleared
        """
        confirm_timer = Timer(1.2, count=3).start()
        cleared = False
        for _ in self.loop():
            if self.appear(AUTO_SEARCH_REWARD, offset=(50, 50), interval=2):
                if drop:
                    if self.ensure_no_info_bar():
                        cleared = True
                    drop.handle_add(main=self, before=4)
                elif self.info_bar_count():
                    # 不记录掉落时只检查当前截图里的清除提示，直接确认奖励。
                    cleared = True
                self.device.click(AUTO_SEARCH_REWARD)
                self.interval_reset([
                    AUTO_SEARCH_REWARD,
                    AUTO_SEARCH_OS_MAP_OPTION_ON,
                    AUTO_SEARCH_OS_MAP_OPTION_OFF,
                    AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED,
                ])
                confirm_timer.reset()
                continue
            if self.handle_map_event():
                confirm_timer.reset()
                continue
            if self.appear_then_click(GLOBE_GOTO_MAP, offset=(20, 20), interval=2):
                # Sometimes entered globe map after clicking AUTO_SEARCH_REWARD
                # because of duplicated clicks and clicks to places outside the map
                confirm_timer.reset()
                continue
            # Donno why but it just entered storage, exit it anyway
            # Equivalent to is_in_storage, but can't inherit StorageHandler here
            # STORAGE_CHECK is a duplicate name, this is the os_handler/STORAGE_CHECK, not handler/STORAGE_CHECK
            if self.appear(STORAGE_CHECK, offset=(20, 20), interval=5):
                logger.info(f'{STORAGE_CHECK} -> {BACK_ARROW}')
                self.device.click(BACK_ARROW)
                confirm_timer.reset()
                continue

            # End
            if self.is_in_map():
                if confirm_timer.reached():
                    break
            else:
                confirm_timer.reset()

        return cleared

    # 开启自律寻敌的连续重试预算（秒）。装置探测演出、剧情收尾期间游戏会持续
    # 数秒不响应该按钮，侵蚀1/短猫按 0.5 秒间隔重试属于有意行为；预算内不判死，
    # 超时才按点击无效上报。
    _os_auto_search_enable_timeout = 45

    def _os_auto_search_enable_click(self, button):
        """开启自律寻敌的带预算重试点击。

        快刷间隔（0.5 秒）下连续重试会攒满「15 次内同一按钮 ≥12 次」的防连点
        阈值，约 7 秒就被误判成卡死并触发游戏重启。这里与剧情选项处理同法：
        点击后清空共用点击记录，改由 `_os_auto_search_enable_timeout` 秒预算
        兜底，超时仍未生效才上报。

        Args:
            button: AUTO_SEARCH_OS_MAP_OPTION_OFF 或 AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED。
        """
        if '_os_auto_search_enable_timer' not in self.__dict__:
            self._os_auto_search_enable_timer = Timer(self._os_auto_search_enable_timeout)
        if not self._os_auto_search_enable_timer.started():
            self._os_auto_search_enable_timer.start()
        elif self._os_auto_search_enable_timer.reached():
            self._os_auto_search_enable_timer.clear()
            raise GameTooManyClickError(
                f'[大世界-搜索] 自律寻敌连续点击 {self._os_auto_search_enable_timeout} 秒仍未生效')
        self.device.click(button)
        # 连续重试会累积共用点击记录，点击后清空；卡死检测由上方预算兜底
        self.device.click_record_clear()

    def _os_auto_search_enable_budget_clear(self):
        """关闭外观消失或界面被剧情挡住时，清零开启自律的重试预算。"""
        timer = self.__dict__.get('_os_auto_search_enable_timer')
        if timer is not None:
            timer.clear()

    def handle_os_auto_search_map_option(self, drop=None, enable=True):
        """
        Args:
            drop (DropImage):
            enable (bool): True/False, or None for doing nothing.

        Returns:
            bool: If clicked.
        """
        command = getattr(getattr(self.config, 'task', None), 'command', None)
        # 侵蚀 1 与耄耋相接是连续刷图：开启自律的点击间隔压到 0.5 秒；
        # 其它任务保留 3 秒的保守间隔。代理执行时 config.task 已是子任务名，
        # 所以智能调度代跑同样命中这里。
        fast_farming = command in ('OpsiHazard1Leveling', 'OpsiMeowfficerFarming')
        if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_OFF, offset=(5, 120)):
            if self.info_bar_count() >= 2:
                self.device.screenshot_interval_set()
                self.os_auto_search_quit(drop=drop)
                raise CampaignEnd
        if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED, offset=(5, 120)):
            if self.info_bar_count() >= 2:
                self.device.screenshot_interval_set()
                self.os_auto_search_quit(drop=drop)
                raise CampaignEnd
        if self.appear(AUTO_SEARCH_REWARD, offset=(50, 50)):
            self.device.screenshot_interval_set()
            cleared = self.os_auto_search_quit(drop=drop)
            if fast_farming and enable is True \
                    and getattr(self, '_os_auto_search_started', False):
                # 正常刷图奖励表示本次搜索已结束，不再开一次空自律探测。
                # 未确认本轮已开启时，奖励可能是上一海域延迟弹出的，保留原恢复。
                # META、退役等中断仍由 os_auto_search_run 的外层恢复分支处理。
                task_name = '侵蚀1' if command == 'OpsiHazard1Leveling' else '耄耋相接'
                logger.info(f'[大世界-搜索] {task_name}奖励已确认，结束本次搜索')
                raise CampaignEnd
            if cleared:
                # No more items on current map
                raise CampaignEnd
            else:
                # Auto search stopped but map hasn't been cleared
                return True

        if enable is None:
            pass
        elif enable:
            # AP 上游对侵蚀 1 / 耄耋相接用 0.5 秒；云环境留余量取 1.0 秒，
            # 其它任务保持 3 秒的保守间隔。45 秒预算兜底不受该值影响。
            click_interval = 1.0 if fast_farming else 3
            if fast_farming:
                # 剧情可能透出地图按钮，先处理剧情；回到地图后才重试开启自律。
                if self.appear(STORY_SKIP_3, offset=(20, 20)):
                    self._os_auto_search_enable_budget_clear()
                    return False
                if not self.is_in_map():
                    self._os_auto_search_enable_budget_clear()
                    return False
            if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_OFF,
                                         offset=(5, 120), interval=click_interval):
                self._os_auto_search_enable_click(AUTO_SEARCH_OS_MAP_OPTION_OFF)
                # 两种关闭外观共用重试间隔，避免按钮变灰后在下一帧重复点击。
                self.get_interval_timer(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED,
                                        interval=click_interval, renew=True).reset()
                return True
            # Game client bugged sometimes, AUTO_SEARCH_OS_MAP_OPTION_OFF grayed out but still functional
            if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED,
                                         offset=(5, 120), interval=click_interval):
                self._os_auto_search_enable_click(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED)
                self.get_interval_timer(AUTO_SEARCH_OS_MAP_OPTION_OFF,
                                        interval=click_interval, renew=True).reset()
                return True
            # 关闭外观消失：自律已开启或界面已切换，重试预算清零
            self._os_auto_search_enable_budget_clear()
        else:
            if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_ON, offset=(5, 120), interval=3):
                self.device.click(AUTO_SEARCH_OS_MAP_OPTION_ON)
                return True

        return False

    def handle_os_map_fleet_lock(self, enable=None):
        """
        Args:
            enable (bool): Default to None, use Campaign_UseFleetLock.

        Returns:
            bool: If switched.
        """
        # Fleet lock depends on if it appear on map, not depends on map status.
        # Because if already in map, there's no map status,
        if not fleet_lock.appear(main=self):
            logger.info('No fleet lock option.')
            return False

        if enable is None:
            enable = self.config.Campaign_UseFleetLock
        state = 'on' if enable else 'off'
        changed = fleet_lock.set(state, main=self)

        return changed
