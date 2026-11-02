from module.base.decorator import Config, cached_property
from module.campaign.campaign_ui import CampaignUI
from module.combat.auto_search_combat import AutoSearchCombat
from module.exception import CampaignEnd, MapEnemyMoved, ScriptEnd, ScriptError
from module.handler.assets import AUTO_SEARCH_MAP_OPTION_OFF, AUTO_SEARCH_MAP_OPTION_ON
from module.logger import logger
from module.map.map import Map
from module.map.map_base import CampaignMap


class CampaignBase(CampaignUI, Map, AutoSearchCombat):
    FUNCTION_NAME_BASE = 'battle_'
    MAP: CampaignMap

    def battle_default(self):
        if self.clear_enemy():
            return True

        logger.warning('No battle executed.')
        return False

    def battle_boss(self):
        if self.brute_clear_boss():
            return True

        logger.warning('No battle executed.')
        return False

    @Config.when(POOR_MAP_DATA=True, MAP_CLEAR_ALL_THIS_TIME=False)
    def battle_function(self):
        logger.info('Using function: battle_with_poor_map_data')
        if self.fleet_2_break_siren_caught():
            return True
        self.clear_all_mystery()

        if self.battle_count >= 3:
            self.pick_up_ammo()

        if self.map.select(is_boss=True):
            if self.brute_clear_boss():
                return True
        else:
            if self.clear_siren():
                return True
            return self.clear_enemy()

        return False

    @Config.when(MAP_CLEAR_ALL_THIS_TIME=True)
    def battle_function(self):
        logger.info('Using function: clear_all')
        if self.fleet_2_break_siren_caught():
            return True
        self.clear_all_mystery()

        if self.battle_count >= 3:
            self.pick_up_ammo()

        remain = self.map.select(is_enemy=True) \
            .add(self.map.select(is_siren=True)) \
            .add(self.map.select(is_fortress=True)) \
            .delete(self.map.select(is_boss=True))
        logger.info(f'Enemy remain: {remain}')
        if remain.count > 0:
            if self.config.MAP_HAS_MOVABLE_NORMAL_ENEMY:
                if self.clear_any_enemy(sort=('cost_2',)):
                    return True
                return self.battle_default()
            else:
                if self.clear_bouncing_enemy():
                    return True
                if self.clear_siren():
                    return True
                self.clear_mechanism()
                return self.battle_default()
        else:
            result = self.battle_boss()
            return result

    @Config.when(MAP_CLEAR_ALL_THIS_TIME=False, POOR_MAP_DATA=False)
    def battle_function(self):
        func = self.FUNCTION_NAME_BASE + 'default'
        for extra_battle in range(10):
            if hasattr(self, self.FUNCTION_NAME_BASE + str(self.battle_count - extra_battle)):
                func = self.FUNCTION_NAME_BASE + str(self.battle_count - extra_battle)
                break

        logger.info(f'Using function: {func}')
        func = self.__getattribute__(func)

        result = func()

        return result

    def execute_a_battle(self):
        logger.hr(f'{self.FUNCTION_NAME_BASE}{self.battle_count}', level=2)
        prev = self.battle_count
        result = False
        for _ in range(10):
            try:
                result = self.battle_function()
                break
            except MapEnemyMoved:
                if self.battle_count > prev:
                    result = True
                    break
                else:
                    continue

        if not result:
            logger.warning('ScriptError, No combat executed.')
            if self.config.Error_HandleError:
                logger.warning('ScriptError, No combat executed, Withdrawing')
                self.withdraw()
            else:
                raise ScriptError('No combat executed.')

        return result

    def run(self):
        logger.hr(self.ENTRANCE, level=2)

        resume = self.__dict__.pop('_resume_after_restart', False)
        if not resume:
            self._recovery_map_active = False
            self._recovery_map_generation = self.__dict__.get('_recovery_map_generation', 0) + 1
            self._restart_stop_requested = False
            self._restart_resume_guard = False
            # Enter map
            self.emotion.check_reduce(self._map_battle)
            self.ENTRANCE.area = self.ENTRANCE.button
            self.enter_map(self.ENTRANCE, mode=self.config.Campaign_Mode)

        # Map init
        if resume:
            logger.info('[战役续接] 保留舰队、等级和战斗次数，继续原关卡')
            if self.__dict__.get('_restart_stop_requested', False):
                try:
                    self.auto_search_withdraw(strict=True)
                except CampaignEnd:
                    pass
                self._recovery_map_active = False
                raise ScriptEnd('[战役续接] 原有停止条件已触发')
        elif not self.map_is_auto_search:
            self.handle_map_fleet_lock()
            self.map_init(self.MAP)
        else:
            self.map = self.MAP
            self.battle_count = 0
            self.lv_reset()
            self.lv_get()

        # Run
        for _ in range(20):
            try:
                if not self.map_is_auto_search:
                    self.execute_a_battle()
                else:
                    self.auto_search_execute_a_battle()
            except CampaignEnd:
                self._recovery_map_active = False
                self._restart_resume_guard = False
                logger.hr('Campaign end')
                return True

        # Exception
        logger.warning('Battle function exhausted.')
        if self.config.Error_HandleError:
            logger.warning('ScriptError, Battle function exhausted, Withdrawing')
            try:
                self.withdraw()
            except CampaignEnd:
                pass
        else:
            raise ScriptError('Battle function exhausted.')

    @cached_property
    def _map_battle(self):
        """
        Returns:
            int: Battle on this map.
        """
        for data in self.MAP.spawn_data:
            if 'boss' in data:
                if 'battle' in data:
                    return data['battle'] + 1
                else:
                    logger.warning('No battle count in spawn_data')

        logger.warning('No boss data found in spawn_data')
        return 0

    def auto_search_execute_a_battle(self):
        logger.hr(f'{self.FUNCTION_NAME_BASE}{self.battle_count}', level=2)
        self._recovery_map_active = True
        self.auto_search_moving()
        self.auto_search_combat(fleet_index=self.fleet_show_index)
        self.battle_count += 1

    def auto_search_withdraw(self, skip_first_screenshot=True, strict=False):
        """自律寻敌运行中撤退。（移植自 AzurPilot，续接回退专用）

        先退出自律寻敌状态回到普通地图，再执行撤退。
        自律寻敌运行时地图右下角的撤退按钮（WITHDRAW）被自律寻敌开关替换，
        直接调用 withdraw() 会一直等不到按钮而卡死。

        Args:
            skip_first_screenshot (bool): 是否跳过首次截图。默认 True。
            strict (bool): 异常续接回退时同时确认开关颜色，避免模板重叠导致反向开启。

        Raises:
            CampaignEnd: 撤退完成时抛出 CampaignEnd('Withdraw')。

        Pages:
            in: is_in_map，自律寻敌运行中或自动搜索菜单
            out: handle_in_stage（撤退完成，raise CampaignEnd('Withdraw')）
        """
        logger.hr('地图撤退（自律寻敌）')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # 自动搜索菜单已弹出（战斗间歇），直接退出，等效撤退
            if self.handle_auto_search_exit():
                continue
            if self.handle_in_stage():
                raise CampaignEnd('Withdraw')

            # 自律寻敌运行中，点击开关关闭
            running = self.is_auto_search_running() if strict \
                else self.appear(AUTO_SEARCH_MAP_OPTION_ON, offset=(5, 5))
            if running \
                    and self.appear_then_click(AUTO_SEARCH_MAP_OPTION_ON, offset=(5, 5), interval=2):
                continue
            # 关闭时可能弹出确认框
            if self.handle_popup_confirm('AUTO_SEARCH_WITHDRAW'):
                continue

            # 开关已回到 OFF，普通地图状态，交给标准撤退流程
            stopped = self.match_template_color(AUTO_SEARCH_MAP_OPTION_OFF, offset=(5, 5), threshold=10) \
                if strict else self.appear(AUTO_SEARCH_MAP_OPTION_OFF, offset=(5, 5))
            if stopped:
                break
        self.withdraw()
