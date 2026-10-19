from module.base.button import Button
from module.base.timer import Timer
from module.combat.assets import GET_ITEMS_1
from module.config.utils import get_os_reset_remain
from module.exception import GameStuckError, ScriptError
from module.logger import logger
from module.os.assets import FLEET_CHOOSE
from module.os_shop.akashi_shop import AkashiShop
from module.os_shop.assets import PORT_SUPPLY_CHECK, SHOP_BUY_CONFIRM
from module.os_shop.port_shop import PortShop
from module.os_shop.ui import OS_SHOP_SCROLL
from module.shop.assets import AMOUNT_MAX, AMOUNT_MINUS, AMOUNT_PLUS, SHOP_BUY_CONFIRM_AMOUNT, SHOP_BUY_CONFIRM as OS_SHOP_BUY_CONFIRM, SHOP_CLICK_SAFE_AREA
from module.shop.clerk import OCR_SHOP_AMOUNT


class OSShop(PortShop, AkashiShop):
    def os_shop_buy_execute(self, button, skip_first_screenshot=True) -> bool:
        """
        Args:
            button: Item to buy
            skip_first_screenshot:

        Pages:
            in: PORT_SUPPLY_CHECK
        """
        success = False
        amount_finish = False
        self.interval_clear([
            PORT_SUPPLY_CHECK, SHOP_BUY_CONFIRM_AMOUNT,
            SHOP_BUY_CONFIRM, OS_SHOP_BUY_CONFIRM, GET_ITEMS_1,
            SHOP_CLICK_SAFE_AREA
        ])
        set_amount_retry = 0

        while True:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.handle_map_get_items(interval=3):
                self.interval_clear(PORT_SUPPLY_CHECK)
                success = True
                continue

            if self.appear_then_click(SHOP_BUY_CONFIRM, offset=(20, 20), interval=3):
                self.interval_reset(SHOP_BUY_CONFIRM)
                continue

            if self.appear_then_click(OS_SHOP_BUY_CONFIRM, offset=(20, 20), interval=3):
                self.interval_reset(OS_SHOP_BUY_CONFIRM)
                continue

            if not amount_finish and self.appear(SHOP_BUY_CONFIRM_AMOUNT, offset=(20, 20)):
                amount_finish = self.shop_buy_amount_handler(button)
                set_amount_retry += 1
                if not amount_finish and set_amount_retry > 3:
                    logger.warning(f'Item {button.name} cant get amount.')
                    self.close_shop_buy_confirm_amount(skip_first_screenshot)
                    break
                continue

            if amount_finish and self.appear_then_click(SHOP_BUY_CONFIRM_AMOUNT, offset=(20, 20), interval=3):
                self.interval_reset(SHOP_BUY_CONFIRM_AMOUNT)
                continue

            if self.handle_popup_confirm('SHOP_BUY'):
                continue

            if not success and self.appear(PORT_SUPPLY_CHECK, offset=(20, 20), interval=5):
                amount_finish = False
                self.device.click(button)
                continue

            # End
            if success and self.appear(PORT_SUPPLY_CHECK, offset=(20, 20)):
                break

        return success

    def os_shop_buy(self, select_func) -> int:
        """
        Args:
            select_func:
                Function to select items to buy.

            in: PORT_SUPPLY_CHECK
        """
        count = 0
        for _ in range(12):
            button = select_func()
            if button is None:
                logger.info('Shop buy finished')
                return count
            else:
                self.os_shop_buy_execute(button)
                count += 1
                continue

        logger.warning('Too many items to buy, stopped')
        return count

    def close_shop_buy_confirm_amount(self, skip_first_screenshot=True):
        """
        Close shop buy confirm amount.

        Args:
            skip_first_screenshot:

        Pages:
            in: SHOP_BUY_CONFIRM_AMOUNT
        """
        self.interval_clear([PORT_SUPPLY_CHECK, SHOP_BUY_CONFIRM_AMOUNT])
        while True:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(PORT_SUPPLY_CHECK, offset=(20, 20)):
                self.interval_clear(SHOP_BUY_CONFIRM_AMOUNT)
                break

            if self.appear(SHOP_BUY_CONFIRM_AMOUNT, offset=(20, 20), interval=3):
                self.device.click(SHOP_CLICK_SAFE_AREA)

    def shop_buy_amount_handler(self, item, skip_first_screenshot=True):
        """
        Handler item amount to buy.

        Args:
            item

        Raises:
            ScriptError: OCR_SHOP_AMOUNT
        """
        limit = -1
        retry = Timer(0, count=3)
        retry.start()
        while True:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            limit = OCR_SHOP_AMOUNT.ocr(self.device.image)

            if limit == 0:
                logger.warning('OCR_SHOP_AMOUNT resulted 0, retrying')
                self.close_shop_buy_confirm_amount()
                return False

            if limit > 0:
                break

            if retry.reached():
                logger.critical('OCR_SHOP_AMOUNT resulted error; '
                                'asset may be compromised')
                raise ScriptError
        retry.reset()


        currency = self.get_currency_coins(item)
        count = min(int(currency // item.price), item.count)

        if count == 1:
            return True

        coins = self.get_coins_no_limit(item)
        total_count = min(int(coins // item.price), item.count)

        set_to_max = False
        # Avg count of all items(no PurpleCoins) is 8.9, so use 10.
        if count <= 10:
            if count - 1 > total_count - count:
                set_to_max = True
            limit = count
        elif total_count - count <= 10:
            set_to_max = True
            limit = count
        elif count >= total_count >> 1:
            set_to_max = True
            limit = total_count - 10
        else:
            limit = 10

        self.interval_clear(AMOUNT_MAX)
        while set_to_max:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(AMOUNT_MAX, offset=(50, 50), interval=3):
                continue

            if OCR_SHOP_AMOUNT.ocr(self.device.image) > 1:
                break

        self.ui_ensure_index(limit, letter=OCR_SHOP_AMOUNT, prev_button=AMOUNT_MINUS, next_button=AMOUNT_PLUS,
                             skip_first_screenshot=True)
        return True

    def handle_port_supply_buy(self) -> bool:
        """
        Returns:
            bool: True if success to buy any or no items found.
                False if not enough coins to buy any.

        Pages:
            in: PORT_SUPPLY_CHECK
        """
        items = self.scan_all()
        if not len(items):
            logger.warning('Empty OS shop.')
            self.config.cross_set("OpsiShop.Storage.Storage.BoughtAllYellowCoinItems", True)
            return False
        items = self.items_filter_in_os_shop(items)
        if not len(items):
            logger.warning('Nothing to buy.')
            self.config.cross_set("OpsiShop.Storage.Storage.BoughtAllYellowCoinItems", True)
            return False
        if all(item.cost == 'PurpleCoins' for item in items):
            logger.info("All yellow coin items are bought.")
            self.config.cross_set("OpsiShop.Storage.Storage.BoughtAllYellowCoinItems", True)
        else:
            logger.info("There are still yellow coin items to buy.")
            self.config.cross_set("OpsiShop.Storage.Storage.BoughtAllYellowCoinItems", False)
        self.os_shop_get_coins()
        skip_get_coins = True
        items.reverse()
        count = 0
        while len(items):
            logger.hr('OpsiShop buy', level=2)
            item = items.pop()
            if not skip_get_coins:
                self.os_shop_get_coins()
            if item.price > self.get_currency_coins(item):
                logger.info(f'Not enough coins to buy item: {item.name}, skip.')
                if self.is_coins_both_not_enough():
                    logger.info('Not enough coins to buy any items, stop.')
                    break
                continue
            logger.info(f'Buying item: {item.name}. In shop {item.shop_index + 1}. At pos {item.scroll_pos:.2f}.')
            self.os_shop_side_navbar_ensure(upper=item.shop_index + 1)
            OS_SHOP_SCROLL.set(item.scroll_pos, main=self, skip_first_screenshot=False)
            _item = self.os_shop_get_items_to_buy(name=item.name, price=item.price)
            if _item is None:
                logger.warning(f'Item {item.name} not found in shop {item.shop_index + 1} at pos {item.scroll_pos:.2f}, skip.')
                continue
            if not self.check_item_count(_item):
                logger.warning(f'Get {_item.name} count error, skip.')
                continue
            if self.os_shop_buy_execute(_item):
                logger.info(f'Bought item: {_item.name}.')
                skip_get_coins = False
                count += 1
            else:
                logger.warning(f'Item {_item.name} cant be bought, skip.')
            self.device.click_record.clear()
        logger.info(f'Bought {f"{count} items" if count else "nothing"} in port.')
        return True

    # The OpSi map is surrounded by fixed UI: fleet bar and its choose button at the
    # bottom left, resource bar on top, radar and the button column on the right, button
    # row at the bottom. Akashi often stands right on top of them, so a click at the grid
    # hits that button instead: the dock / the fleet bar / the voucher shop opens and the
    # shop itself never does -- the event then looks "handled" while it was missed.
    AKASHI_CLICK_SAFE_AREA = (239, 128, 993, 628)  # left, top, right, bottom, in 1280x720
    AKASHI_CLICK_MIN_SIZE = 6

    @staticmethod
    def _akashi_safe_click_button(grid):
        """将点击限制在地图安全范围内，避免格子下沿落入固定操作栏。"""
        try:
            x1, y1, x2, y2 = grid.button
        except (AttributeError, TypeError, ValueError):
            return None
        left, top, right, bottom = OSShop.AKASHI_CLICK_SAFE_AREA
        area = (max(left, x1), max(top, y1), min(right, x2), min(bottom, y2))
        if area[2] - area[0] < OSShop.AKASHI_CLICK_MIN_SIZE \
                or area[3] - area[1] < OSShop.AKASHI_CLICK_MIN_SIZE:
            return None
        return Button(area=area, color=(), button=area, name=f'AKASHI_{grid}')

    def _recover_akashi_page(self):
        """只处理正向识别到的误入页面；操作后由父循环获取新截图。"""
        for check, leave in (
                ('is_in_globe', 'os_globe_goto_map'),
                ('is_in_storage', 'storage_quit'),
                ('is_in_os_mission', 'os_mission_quit'),
                ('is_in_map_order', 'order_quit')):
            if hasattr(self, check) and getattr(self, check)():
                logger.info(f'[大世界-明石] 误入页面 {check}，返回地图后重新定位')
                getattr(self, leave)()
                return True
        if self.is_in_map() and hasattr(self, 'fleet_selector') \
                and self.fleet_selector.bar_opened():
            return self.appear_then_click(FLEET_CHOOSE, offset=(20, 20), interval=2)
        return False

    def handle_akashi_supply_buy(self, grid) -> bool:
        """打开明石商店、购买并返回地图。

        入口点击夹进地图安全区并避让固定按钮；预算用尽仍没进店时返回 False，
        调用方不得把失败记为已处理，否则这只明石就永久漏了。

        Args:
            grid: Grid where akashi stands, or any button when the walk already opened
                the shop (`PORT_SUPPLY_CHECK` is on screen).

        Returns:
            bool: 是否确认进入商店、完成购买流程并返回地图。

        Pages:
            in: is_in_map 或 PORT_SUPPLY_CHECK
            out: 成功为 is_in_map；入口失败时保留当前画面，交由调用方恢复。
        """
        remember = getattr(self, '_remember_pending_akashi', None)
        if remember is not None:
            remember(grid)
        locate = getattr(self, '_akashi_global_location', None)
        target = locate(grid) if locate is not None else None
        no_progress = Timer(12).start()
        click_timer = Timer(2)
        clicks = 0
        page_recoveries = 0
        camera_budget = [2]
        for _ in self.loop():
            if self.appear(PORT_SUPPLY_CHECK, offset=(20, 20)):
                break
            if no_progress.reached():
                logger.warning(f'[大世界-明石] 入口无进展，已点击 {clicks}/3 次')
                return False
            if self.handle_story_skip():
                no_progress.reset()
                continue
            if self._recover_akashi_page():
                page_recoveries += 1
                if page_recoveries > 4:
                    logger.warning('[大世界-明石] 入口反复误入其他页面，交回地图恢复')
                    return False
                continue
            # 下拉菜单的关闭按钮有冷却，等待期间不能穿过菜单继续点格子。
            if hasattr(self, 'fleet_selector') and self.fleet_selector.bar_opened():
                continue
            if self.is_in_map() and clicks < 3 and click_timer.reached():
                if target is not None:
                    button = self._prepare_akashi_click(target, camera_budget=camera_budget)
                else:
                    button = self._akashi_safe_click_button(grid)
                # 定位期间的新截图可能已经确认进入商店，不能再盲点原目标。
                if self.appear(PORT_SUPPLY_CHECK, offset=(20, 20)):
                    break
                if button is None:
                    logger.warning('[大世界-明石] 无可用安全点击区域，交回地图恢复')
                    return False
                if no_progress.reached():
                    return False
                self.device.click(button)
                clicks += 1
                click_timer.reset()
        self.os_shop_buy(select_func=self.os_shop_get_item_to_buy_in_akashi)
        self.ui_back(appear_button=PORT_SUPPLY_CHECK, check_button=self.is_in_map,
                     skip_first_screenshot=True)
        if not self.is_in_map():
            logger.warning('[大世界-明石] 购买结束但没回到地图，本次明石记为未完成')
            return False
        finish = getattr(self, '_finish_pending_akashi', None)
        if finish is not None:
            finish()
        return True

    def get_currency_coins(self, item):
        if item.cost == 'YellowCoins':
            if get_os_reset_remain() == 0:
                return self._shop_yellow_coins - 100
            else:
                return self._shop_yellow_coins - self.yellow_coins_preserve

        elif item.cost == 'PurpleCoins':
            if get_os_reset_remain() == 0:
                return self._shop_purple_coins
            else:
                return self._shop_purple_coins - self.config.OS_NORMAL_PURPLE_COINS_PRESERVE

    def get_coins_no_limit(self, item):
        if item.cost == 'YellowCoins':
            return self._shop_yellow_coins
        elif item.cost == 'PurpleCoins':
            return self._shop_purple_coins

    def is_coins_both_not_enough(self):
        if get_os_reset_remain() == 0:
            return False
        else:
            yellow = self._shop_yellow_coins < self._shop_purple_coins
            purple = self._shop_purple_coins < self.config.OS_NORMAL_PURPLE_COINS_PRESERVE
            return yellow and purple
