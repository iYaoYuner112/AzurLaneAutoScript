import typing as t
from datetime import datetime, timedelta
from time import monotonic

from module.base.decorator import cached_property
import module.config.server as server
from module.base.timer import Timer
from module.config.config import Function
from module.config.utils import get_server_next_update
from module.logger import logger
from module.map.map_grids import SelectedGrids
from module.ocr.ocr import Digit
from module.os.tasks.task_context import is_running_opsi_proxy
from module.os_shop.assets import OS_SHOP_CHECK, OS_SHOP_PURPLE_COINS, SHOP_PURPLE_COINS, SHOP_YELLOW_COINS
from module.statistics.resource_monitor import record_dashboard_resource
from module.ui.ui import UI

if server.server != 'jp':
    OCR_SHOP_YELLOW_COINS = Digit(SHOP_YELLOW_COINS, letter=(239, 239, 239), threshold=160, name='OCR_SHOP_YELLOW_COINS')
else:
    OCR_SHOP_YELLOW_COINS = Digit(SHOP_YELLOW_COINS, letter=(201, 201, 201), threshold=200, name='OCR_SHOP_YELLOW_COINS')
OCR_SHOP_PURPLE_COINS = Digit(SHOP_PURPLE_COINS, letter=(255, 255, 255), name='OCR_SHOP_PURPLE_COINS')
OCR_OS_SHOP_PURPLE_COINS = Digit(OS_SHOP_PURPLE_COINS, letter=(255, 255, 255), name='OCR_OS_SHOP_PURPLE_COINS')


class OSStatus(UI):
    _shop_yellow_coins = 0
    _shop_purple_coins = 0
    # 黄币读数的可信度。连续两次 OCR 一致才算"新鲜"；OCR 失败时沿用上次读数，
    # 但过了 YELLOW_COINS_MAX_AGE（5 分钟）就作废，旧读数不能当成现在的余额。
    YELLOW_COINS_MAX_AGE = 300
    _yellow_coins_updated_at = 0.0
    _yellow_coins_fresh = False
    _yellow_coins_source = 'none'

    @property
    def is_in_task_explore(self) -> bool:
        return self.config.task.command == 'OpsiExplore'

    @property
    def is_in_task_cl1_leveling(self) -> bool:
        # Standalone CL1 only. When smart scheduling proxies the CL1 work, it
        # runs as a sub-task, and `map_rescan` must NOT be skipped there (see
        # OSMap.map_rescan); keeping the proxy out of this identity preserves
        # that behaviour while `config.task` now carries the sub-task name.
        if is_running_opsi_proxy(self.config):
            return False
        return self.config.task.command == 'OpsiHazard1Leveling'

    @property
    def is_cl1_enabled(self) -> bool:
        return self.config.is_task_enabled('OpsiHazard1Leveling')

    @property
    def is_smart_scheduling_enabled(self) -> bool:
        """智能调度是否生效；开荒未完成时视为未启用（对齐 AzurPilot）。

        开荒期间返回 False，让侵蚀1/短猫等依赖此判断的子任务按「非代理模式」运行，
        不再以为调度在代跑。
        """
        # 检测是否在开荒中，如果是，则停止智能调度
        if self.is_in_opsi_explore():
            return False
        return self.config.is_task_enabled('OpsiScheduling')

    @property
    def is_cl1_mode_enabled(self) -> bool:
        return self.is_cl1_enabled or self.is_smart_scheduling_enabled

    @staticmethod
    def task_cooling_soon(task, now, update) -> bool:
        """
        Whether this task is cooling down and will be ready within an hour.

        A task whose `next_run` has already passed is not cooling down. Handing a past time to
        `task_delay(target=...)` makes the waiting task run again in the same round, and a
        non-None result here also blocks calling OpsiObscure / OpsiAbyssal / OpsiStronghold
        (AzurPilot d43a1361e, 「修复大世界可能在某个任务死循环的问题」).

        Args:
            task (Function):
            now (datetime):
            update (datetime): Next server update.

        Returns:
            bool:
        """
        if not (task.command in ('OpsiObscure', 'OpsiAbyssal', 'OpsiStronghold', 'OpsiDaily')
                and task.enable):
            return False
        if task.next_run == update:
            # Scheduled at the daily reset: the scheduler handles it, it is not a cooldown.
            return False
        return now < task.next_run <= now + timedelta(minutes=60)

    @property
    def nearest_task_cooling_down(self) -> t.Optional[Function]:
        """
        If having any tasks cooling down,
        such as recon scan cooldown and submarine call cooldown.
        """
        now = datetime.now()
        update = get_server_next_update('00:00')

        tasks = SelectedGrids(self.config.pending_task + self.config.waiting_task) \
            .filter(lambda task: self.task_cooling_soon(task, now, update)).sort('next_run')
        return tasks.first_or_none()

    @property
    def bought_all_yellow_coin_items_in_port_shop(self) -> bool:
        return self.config.cross_get("OpsiShop.Storage.Storage.BoughtAllYellowCoinItems", False)

    @cached_property
    def yellow_coins_preserve(self):
        if self.is_smart_scheduling_enabled:
            return self.config.OpsiScheduling_OperationCoinsPreserve
        if self.is_cl1_enabled and not self.bought_all_yellow_coin_items_in_port_shop:
            return 100000
        else:
            return 35000

    @property
    def yellow_coins_source(self):
        """
        Where the last yellow coin reading came from.

        Returns:
            str: ocr (two equal readings), ocr-once (not confirmed), reuse
                (OCR failed, last reading reused), none (no usable reading).
        """
        return self._yellow_coins_source

    def yellow_coins_fresh(self, max_age=None):
        """
        Whether the last yellow coin reading can be trusted for a decision.

        Args:
            max_age (int, optional): Seconds before a reading is too old.

        Returns:
            bool: True if two consecutive OCR readings agreed and it is not old.
        """
        if not self._yellow_coins_fresh or not self._yellow_coins_updated_at:
            return False
        if max_age is None:
            max_age = self.YELLOW_COINS_MAX_AGE
        return monotonic() - self._yellow_coins_updated_at <= max_age

    def _record_yellow_coins(self, value, fresh, source):
        """
        Store a yellow coin reading together with its credibility.

        Args:
            value (int): Yellow coins.
            fresh (bool): Two consecutive OCR readings agreed.
            source (str): Where the reading came from.
        """
        self._shop_yellow_coins = int(value)
        self._yellow_coins_updated_at = monotonic()
        self._yellow_coins_fresh = bool(fresh)
        self._yellow_coins_source = source
        logger.attr('YellowCoins', f'{self._shop_yellow_coins} ({source})')

    def get_yellow_coins(self, timeout=3) -> int:
        """
        Read yellow coins, with a credibility check.

        黄币靠截图 OCR，慢设备或界面动画没走完时会读到旧值或 0。这里要求连续
        两次读数一致才算新鲜；OCR 一直失败时沿用上次读数（带时间戳，过期作废），
        不要把 0 当成真实余额交给调度。

        Args:
            timeout (int): Seconds to keep trying for a confirmation.

        Returns:
            int: Yellow coins.
        """
        timer = Timer(timeout, count=3).start()
        previous = None
        value = None
        confirmed = False
        for _ in self.loop():
            value = OCR_SHOP_YELLOW_COINS.ocr(self.device.image)
            if value >= 100:
                if previous is not None and value == previous:
                    confirmed = True
                    break
                previous = value
            else:
                # OCR may get 0 or 1 when the amount is not loaded yet
                logger.info('Yellow coins less than 100, assuming it is an ocr error')
                previous = None
            if timer.reached():
                logger.warning('Get yellow coins timeout')
                break

        if confirmed:
            self._record_yellow_coins(value, fresh=True, source='ocr')
        elif value is not None and value >= 100:
            # 只确认了一次（或每次都不一致）：能用，但不标记为新鲜
            self._record_yellow_coins(value, fresh=False, source='ocr-once')
        else:
            age = monotonic() - self._yellow_coins_updated_at
            if self._yellow_coins_updated_at and age <= self.YELLOW_COINS_MAX_AGE:
                logger.info(f'Yellow coins OCR failed, reuse the reading {age:.0f}s ago: '
                            f'{self._shop_yellow_coins}')
                self._yellow_coins_source = 'reuse'
                return self._shop_yellow_coins
            logger.warning('Yellow coins OCR failed and no recent reading, assume 0')
            self._yellow_coins_source = 'none'
            self._shop_yellow_coins = 0
            return 0

        record_dashboard_resource(self.config, 'YellowCoin', self._shop_yellow_coins)
        return self._shop_yellow_coins

    def get_yellow_coins_for_decision(self):
        """
        Yellow coins for a scheduling decision: re-read once when not fresh.

        Returns:
            int: Yellow coins to decide with.
        """
        yellow_coins = self.get_yellow_coins()
        if self.yellow_coins_fresh():
            return yellow_coins
        logger.info('[大世界-调度] 黄币读数不新鲜（未连续两次一致），决策前重读一次')
        yellow_coins = self.get_yellow_coins()
        if not self.yellow_coins_fresh():
            logger.warning(f'[大世界-调度] 黄币读数仍不新鲜（来源={self.yellow_coins_source}），'
                           f'按 {yellow_coins} 决策')
        return yellow_coins

    def get_purple_coins(self) -> int:
        if self.appear(OS_SHOP_CHECK):
            purple_coins = OCR_OS_SHOP_PURPLE_COINS.ocr(self.device.image)
        else:
            purple_coins = OCR_SHOP_PURPLE_COINS.ocr(self.device.image)
        record_dashboard_resource(self.config, 'PurpleCoin', purple_coins)
        return purple_coins

    def os_shop_get_coins(self):
        self._shop_yellow_coins = self.get_yellow_coins()
        self._shop_purple_coins = self.get_purple_coins()
        logger.info(f'Yellow coins: {self._shop_yellow_coins}, purple coins: {self._shop_purple_coins}')
