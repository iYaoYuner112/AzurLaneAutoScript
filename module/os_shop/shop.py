from module.base.button import Button
from module.base.timer import Timer
from module.combat.assets import GET_ITEMS_1
from module.config.utils import get_os_reset_remain
from module.exception import GameStuckError, ScriptError
from module.logger import logger
from module.os_shop.akashi_shop import AkashiShop
from module.os_shop.assets import PORT_SUPPLY_CHECK, SHOP_BUY_CONFIRM
from module.os_shop.port_shop import PortShop
from module.os_shop.ui import OS_SHOP_SCROLL
from module.shop.assets import AMOUNT_MAX, AMOUNT_MINUS, AMOUNT_PLUS, SHOP_BUY_CONFIRM_AMOUNT, SHOP_BUY_CONFIRM as OS_SHOP_BUY_CONFIRM, SHOP_CLICK_SAFE_AREA
from module.shop.clerk import OCR_SHOP_AMOUNT

# 明石常刷新在地图边缘，和左下角舰队选择栏、右侧按钮等固定 UI 重合。点击之前把
# 实际点击框夹进这块地图安全区：不夹的话，点下去命中的是船坞/舰队面板/白票商店，
# 商店没开、事件却被记成"已处理"，这只猫就永久漏掉了。
AKASHI_CLICK_SAFE_AREA = (239, 128, 993, 628)
# 夹完比这个还小就等于整块被固定 UI 压住，宁可不点（点了也只会命中 UI）。
AKASHI_CLICK_MIN_SIZE = 6
# 进店循环自带超时。ui_click 是 while 1 且没有超时，点不开就一直点，攒够设备
# 防连点次数就抛 GameTooManyClickError、判卡死、重启游戏。
AKASHI_ENTER_TIMEOUT = 20
# 点击框完全被固定 UI 压住时，挪镜头重新识别的次数。
AKASHI_FOCUS_RETRIES = 2


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

        # find AMOUNT_MINUS AMOUNT_PLUS
        index_offset = (40, 20)
        for _ in self.loop(timeout=1):
            if self.appear(AMOUNT_MINUS, offset=index_offset) and self.appear(AMOUNT_PLUS, offset=index_offset):
                break
        else:
            logger.warning('Wait AMOUNT_MINUS and AMOUNT_PLUS timeout, continue anyway')

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

    @staticmethod
    def akashi_safe_click_button(grid):
        """
        Clamp Akashi's click box into the map safe area.

        Akashi often stands at the edge of the map, on top of the fixed UI (the
        fleet selector bar on the left bottom, the buttons on the right). Clicking
        its raw box then hits that UI instead: the shop never opens, but the map
        event was already marked as handled, so this Akashi is lost for the reset.

        Args:
            grid: Grid where Akashi stands (a Button-like object with a click box).

        Returns:
            Button | None: Button to click; None when the box is fully covered by
                fixed UI, or when the object has no click box at all.
        """
        area = getattr(grid, 'button', None)
        if not area or len(area) != 4:
            return None
        left = max(int(area[0]), AKASHI_CLICK_SAFE_AREA[0])
        top = max(int(area[1]), AKASHI_CLICK_SAFE_AREA[1])
        right = min(int(area[2]), AKASHI_CLICK_SAFE_AREA[2])
        bottom = min(int(area[3]), AKASHI_CLICK_SAFE_AREA[3])
        if right - left < AKASHI_CLICK_MIN_SIZE or bottom - top < AKASHI_CLICK_MIN_SIZE:
            logger.info(f'Akashi {grid} click box {tuple(area)} is covered by fixed UI')
            return None
        return Button(area=(left, top, right, bottom), color=(0, 0, 0),
                      button=(left, top, right, bottom), name='AKASHI_SAFE_CLICK')

    def akashi_recover_page(self):
        """
        Leave a page that a click on Akashi opened by accident.

        The click box sits on the map edge, so a missed click opens the globe,
        the storage, an OS mission or the map order instead of the shop, and the
        fleet bar drops down when the click lands on the fleet selector.

        Returns:
            bool: True if a page was left; the caller should screenshot again.
        """
        if self.is_in_globe():
            logger.info('Akashi click opened the globe, back to the map')
            self.os_globe_goto_map()
            return True
        if self.is_in_storage():
            logger.info('Akashi click opened the storage, quit')
            self.storage_quit()
            return True
        if self.is_in_os_mission():
            logger.info('Akashi click opened an OS mission, quit')
            self.os_mission_quit()
            return True
        if self.is_in_map_order():
            logger.info('Akashi click opened the map order, quit')
            self.order_quit()
            return True
        selector = getattr(self, 'fleet_selector', None)
        if selector is not None and selector.bar_opened():
            logger.info('Akashi click opened the fleet bar, close it')
            selector.close()
            return True
        return False

    def akashi_prepare_click(self, grid):
        """
        Build the click box for Akashi, moving the camera when it is fully covered.

        Args:
            grid: Grid where Akashi stands.

        Returns:
            Button | None: Clamped button to click, None if no usable box was found.
        """
        button = self.akashi_safe_click_button(grid)
        if button is not None:
            return button
        for _ in range(AKASHI_FOCUS_RETRIES):
            logger.info(f'Akashi {grid} is covered by fixed UI, move the camera and retry')
            self.focus_to(grid, swipe_limit=(3, 2))
            self.update()
            grids = self.view.select(is_akashi=True)
            if not grids or not grids[0].is_akashi:
                logger.info('Akashi is not in the new view, give up clicking it')
                return None
            grid = grids[0]
            button = self.akashi_safe_click_button(grid)
            if button is not None:
                return button
        return None

    def akashi_enter_shop(self, button):
        """
        Click Akashi until the supply shop opens, bounded by AKASHI_ENTER_TIMEOUT.

        Args:
            button (Button): Click box built by akashi_prepare_click().

        Returns:
            bool: True if the shop is open.
        """
        self.device.screenshot()
        if self.appear(PORT_SUPPLY_CHECK, offset=(20, 20)):
            return True
        self.interval_clear(PORT_SUPPLY_CHECK)
        if self.is_in_map():
            self.device.click(button)
        click_timer = Timer(2, count=2).start()
        timeout = Timer(AKASHI_ENTER_TIMEOUT, count=3).start()
        while 1:
            self.device.screenshot()
            if self.appear(PORT_SUPPLY_CHECK, offset=(20, 20)):
                return True
            if timeout.reached():
                logger.warning(f'Akashi shop did not open in {AKASHI_ENTER_TIMEOUT}s')
                return False
            if self.akashi_recover_page():
                click_timer.reset()
                continue
            if self.handle_story_skip():
                click_timer.reset()
                continue
            if self.is_in_map() and click_timer.reached():
                self.device.click(button)
                click_timer.reset()

    def handle_akashi_supply_buy(self, grid):
        """
        Buy from Akashi, tolerating a click box that overlaps the fixed UI.

        The whole sequence is bounded: a click that never opens the shop used to
        keep ui_click() spinning until the game's anti-click detector restarted
        the game, and the event was marked solved anyway.

        Args:
            grid: Grid where Akashi stands, or the safe button when the shop is
                already open (walked into the shop event).

        Returns:
            bool: True only when the shop was entered, the buying finished and
                the map is visible again.
        """
        self.device.screenshot()
        if not self.appear(PORT_SUPPLY_CHECK, offset=(20, 20)):
            button = self.akashi_prepare_click(grid)
            if button is None:
                logger.warning(f'Akashi {grid} cannot be clicked, skip the supply buy')
                return False
            if not self.akashi_enter_shop(button):
                return False
        self.os_shop_buy(select_func=self.os_shop_get_item_to_buy_in_akashi)
        self.ui_back(appear_button=PORT_SUPPLY_CHECK, check_button=self.is_in_map,
                     skip_first_screenshot=True)
        if not self.is_in_map():
            logger.warning('Akashi supply buy did not get back to the map')
            return False
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
