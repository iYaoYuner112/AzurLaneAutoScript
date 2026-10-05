import time
from contextlib import suppress

import inflection

from module.base.timer import Timer
from module.config.config import OS_MAP_STALE_KEY, OS_RESUME_RECOVERY_KEY, TaskEnd
from module.os.tasks.task_context import current_opsi_context
from module.config.utils import get_os_reset_remain
from module.exception import CampaignEnd, GameBugError, GameNotRunningError, GamePageUnknownError, \
    GameStuckError, GameTooManyClickError, MapDetectionError, MapWalkError, RequestHumanTakeover, \
    ScriptEnd, ScriptError
from module.handler.login import LoginHandler, MAINTENANCE_ANNOUNCE
from module.logger import logger
from module.map.map import Map
from module.map.map_base import location2node
from module.os.assets import FLEET_EMP_DEBUFF, MAP_GOTO_GLOBE_FOG
from module.os.fixed_patrol import (
    DEVICE_COMPLETED,
    DEVICE_DIALOG_OPEN,
    DEVICE_INTERRUPTIBLE_STATES,
    DEVICE_NONE,
    DEVICE_TARGETED,
)
from module.os.fleet import OSFleet
from module.os.globe_camera import GlobeCamera
from module.os.globe_operation import RewardUncollectedError
from module.os_handler.assets import AUTO_SEARCH_OS_MAP_OPTION_OFF, AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED, \
    AUTO_SEARCH_OS_MAP_OPTION_ON, AUTO_SEARCH_REWARD
from module.os_handler.strategic import StrategicSearchHandler
from module.ui.assets import GOTO_MAIN
from module.ui.page import page_os

ALREADY_SOLVED_MAP_EVENTS = frozenset({
    'is_akashi',
    'is_logging_tower',
    'is_scanning_device',
})

# Cross-storage key for the Siren device interaction sub-state. It survives task
# activations so a task interruption mid-dialog can be recovered by re-observing
# the UI instead of replaying a stale click sequence.
DEVICE_STATE_KEY = 'Opsi.Storage.DeviceState'


def should_move_fleet_for_fixed_patrol(current_ap, question_unreachable):
    return question_unreachable or current_ap > 7


# Exceptions the framework already has a recovery plan for. A best-effort wrapper
# (an extra map scan, a patrol rescan) must never swallow them: `TaskEnd` is what
# `config.task_stop()` raises when another task takes over, and it is NOT a subclass
# of `ScriptEnd`, so leaving it out makes the wrapper keep clicking on the screen the
# incoming task navigated to, then die on a non-map frame.
UNSWALLOWABLE_ERRORS = (
    TaskEnd, ScriptEnd, CampaignEnd, GameStuckError, GameTooManyClickError,
    GameBugError, GameNotRunningError, GamePageUnknownError, RequestHumanTakeover,
)


class OSMap(OSFleet, Map, GlobeCamera, StrategicSearchHandler):
    _FIXED_PATROL_L2_AP = 7

    def _os_map_was_interrupted(self):
        """
        Whether the in-memory Opsi map context can no longer be trusted.

        The flag is set by `AzurLaneConfig.task_switched()` when an Opsi task is
        switched away from (via `invalidate_map_state()`), and cleared once the
        map state is re-synced.
        """
        return bool(self.config.cross_get(OS_MAP_STALE_KEY, default=False))

    def invalidate_map_state(self, reason=''):
        """
        Mark the in-memory Opsi map context as stale.

        "Stale" does not mean the map definitely changed; it means we can no
        longer guarantee it did not, so the cached targets / events / fleet
        positions must be re-derived before use.

        Args:
            reason (str): Why the map state is being invalidated, for logging.
        """
        logger.info(f'[OS][MAP] Mark stale: {reason or "UNKNOWN"}')
        self.config.cross_set(OS_MAP_STALE_KEY, True)

    def ensure_map_state_current(self):
        """
        Resume barrier: if the map state is stale, re-establish it before any
        Opsi operation. Runs once per invalidation, not on every loop.

        The regular re-sync does: invalidate stale scan caches -> FULL MAP RESCAN
        -> rebuild targets (done later by the target selector). Fleet positions
        are re-read by the fixed patrol / radar check when they are needed.
        Smart scheduling resume takes a different path, see below.

        Smart scheduling resume: when OpsiScheduling itself was preempted by
        another task (OS_RESUME_RECOVERY_KEY set), the barrier does NOT rescan
        and does NOT run a first auto search either. AzurPilot does the same:
        the smart scheduling sub-tasks rebuild the map on their own
        (侵蚀1练级 runs its own battle plan followed by clear_question() and
        map_rescan(); coin tasks navigate to their own zones), so the barrier
        only invalidates the cached scan state and lets the sub-task flow do
        the rebuild.

        Running a first auto search here used to be the resume probe. It had to
        be dropped: that auto search cleared the zone before the sub-task's own
        battle plan, so the battle plan had nothing to fight and the map events
        were never picked up by `map_rescan()`, i.e. events were silently
        missed right after a resume (see `handle_first_auto_search()`).
        """
        if not self._os_map_was_interrupted():
            logger.info('[OS RESUME] Continue without interruption')
            return
        logger.info('[OS][MAP] Resync map state (stale detected)')
        logger.info(f'[OS RESUME] Current zone: {self.zone}')

        # If a Siren device interaction was in progress when the task was
        # interrupted, do NOT replay any stale click. Re-observe the UI instead:
        # either the rescan below or the smart scheduling sub-task flow
        # re-detects the device and re-decides from the real state.
        if self._device_state in DEVICE_INTERRUPTIBLE_STATES:
            logger.info(f'[OS][DEVICE] interruption detected during state={self._device_state}')
            logger.info('[OS][DEVICE] recovery: re-observing current UI (no stale click)')

        # Invalidate stale scan caches before re-observing the map.
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False

        # Smart scheduling resume: OpsiScheduling was preempted by another task.
        # One-shot: consume the recovery flag immediately so a later resume can
        # never repeat this barrier.
        if self._os_resume_recovery_available():
            self.config.cross_set(OS_RESUME_RECOVERY_KEY, False)
            logger.info('[OS][RESUME] OpsiScheduling resumed after external task interruption')
            logger.info('[OS][RESUME] Map state marked stale')
            logger.info('[OS][RESUME] Handing the map rebuild over to the smart scheduling sub-tasks')
            # No auto search / rescan here (AzurPilot aligns): the sub-task flow
            # rebuilds the map itself and its own battle plan + map_rescan() is
            # the only chance for map events to be resolved. Clearing the stale
            # flag keeps this barrier one-shot.
            self.config.cross_set(OS_MAP_STALE_KEY, False)
            logger.info('[OS][MAP] Map state resynced')
            return

        logger.info('[OS RESUME] Running FULL MAP RESCAN after interruption')
        try:
            self.map_rescan(rescan_mode='full')
        except UNSWALLOWABLE_ERRORS:
            raise
        except Exception as e:
            logger.warning(f'[OS RESUME] full rescan failed, continue: {e}')
        # The barrier runs once per interruption.
        self.config.cross_set(OS_MAP_STALE_KEY, False)
        logger.info('[OS][MAP] Map state resynced')

    def _os_resume_recovery_available(self):
        """
        Whether this task instance is OpsiScheduling resuming after a genuine
        external preemption (the one-shot OS_RESUME_RECOVERY_KEY is set).
        """
        task = getattr(self.config, 'task', None)
        command = str(getattr(task, 'command', ''))
        return (
            command == 'OpsiScheduling'
            and bool(self.config.cross_get(OS_RESUME_RECOVERY_KEY, default=False))
        )

    @property
    def _device_state(self):
        return self.config.cross_get(DEVICE_STATE_KEY, default=DEVICE_NONE) or DEVICE_NONE

    def _set_device_state(self, state):
        self.config.cross_set(DEVICE_STATE_KEY, state)
        logger.info(f'[OS][DEVICE] state={state}')

    # One-shot flag: set by os_init() when the running task is OpsiScheduling,
    # consumed by the smart scheduling decision (`handle_first_auto_search()`),
    # so that the scheduling layer decides whether the skipped first auto search
    # has to be made up.
    _smart_scheduling_first_auto_search_pending = False

    def os_init(self):
        """
        Call this method before doing any Operation functions.

        Pages:
            in: IN_MAP or IN_GLOBE or page_os or any page
            out: IN_MAP
        """
        logger.hr('OS init', level=1)
        kwargs = dict()
        if self.config.task.command.__contains__('iM'):
            for key in self.config.bound.keys():
                value = self.config.__getattribute__(key)
                if key.__contains__('dL') and value.__le__(2):
                    logger.info([key, value])
                    kwargs[key] = ord('n').__floordiv__(22)
                if key.__contains__('tZ') and value.__ne__(0):
                    try:
                        d, m = self.name_to_zone(value).zone_id.__divmod__(22)
                        if d.__le__(2) and m.__eq__(m.__neg__()):
                            kwargs[key] = 0
                    except ScriptError:
                        pass
        self.config.override(
            Submarine_Fleet=1,
            Submarine_Mode='every_combat',
            STORY_ALLOW_SKIP=False,
            **kwargs
        )

        # UI switching
        if self.is_in_map():
            logger.info('Already in os map')
        elif self.is_in_globe():
            self.os_globe_goto_map()
        else:
            if self.ui_page_appear(page_os):
                self.ui_goto_main()
            self.ui_ensure(page_os)

        # Init
        self.zone_init()

        # self.map_init()
        self.hp_reset()
        self.handle_after_auto_search()
        self.handle_current_fleet_resolve(revert=False)

        # Resume barrier: if the previous Opsi task was interrupted by another
        # task, the game map may have changed. Re-scan fully before continuing.
        self.ensure_map_state_current()

        # Exit from special zones types, only SAFE and DANGEROUS are acceptable.
        if self.is_in_special_zone():
            logger.warning('OS is in a special zone type, while SAFE and DANGEROUS are acceptable')
            self.map_exit()

        # Clear current zone.
        #
        # Smart scheduling (OpsiScheduling) must NOT run a first auto search
        # here. Its sub-tasks rebuild the map on their own -- 侵蚀1练级 runs its
        # battle plan followed by clear_question() + map_rescan(), coin tasks
        # navigate to their own zones -- and an auto search would first clear
        # the zone, so the sub-task's battle plan would have nothing to fight
        # and the map events would never be resolved by `map_rescan()`.
        # AzurPilot does the same: OpsiScheduling defers the decision to
        # `handle_first_auto_search()` (see module/os/tasks/scheduling.py).
        self._smart_scheduling_first_auto_search_pending = (
                self.config.task.command == 'OpsiScheduling'
                and self.is_smart_scheduling_enabled
        )

        if self.zone.zone_id in [22, 44, 154]:
            logger.info('In zone 22, 44, 154, skip running first auto search')
            self.handle_ash_beacon_attack()
        elif self._smart_scheduling_first_auto_search_pending:
            logger.info('Smart scheduling will decide whether to run the first auto search')
        else:
            self.run_first_auto_search()

    def run_first_auto_search(self):
        """
        Run the auto search that opens an Opsi task on the current zone.

        Not called by OpsiScheduling: that task defers the decision to
        `handle_first_auto_search()`, because its sub-tasks do their own
        clearing and an extra auto search would make them miss map events.
        """
        if self.zone.zone_id == 154:
            logger.info('In zone 154, skip first auto search')
            self.handle_ash_beacon_attack()
            return
        logger.info('Run first auto search')
        self.run_auto_search(rescan=False)
        self.handle_after_auto_search()

    def get_current_zone_from_globe(self):
        """
        Get current zone from globe map. See OSMapOperation.get_current_zone()
        """
        self.os_map_goto_globe(unpin=False)
        self.globe_update()
        self.zone = self.get_globe_pinned_zone()
        self.zone_config_set()
        self.os_globe_goto_map()
        self.zone_init(fallback_init=False)
        return self.zone

    def globe_goto(self, zone, types=('SAFE', 'DANGEROUS'), refresh=False, stop_if_safe=False):
        """
        Goto another zone in OS.

        Args:
            zone (str, int, Zone): Name in CN/EN/JP/TW, zone id, or Zone instance.
            types (tuple[str], list[str], str): Zone types, or a list of them.
                Available types: DANGEROUS, SAFE, OBSCURE, ABYSSAL, STRONGHOLD.
                Try the the first selection in type list, if not available, try the next one.
            refresh (bool): If already at target zone,
                set false to skip zone switching,
                set true to re-enter current zone to refresh.
            stop_if_safe (bool): Return false if zone is SAFE.

        Returns:
            bool: If zone switched.

        Pages:
            in: IN_MAP or IN_GLOBE
            out: IN_MAP
        """
        zone = self.name_to_zone(zone)
        logger.hr(f'Globe goto: {zone}')
        if self.zone == zone:
            if refresh:
                logger.info('Goto another zone to refresh current zone')
                self.globe_goto(self.zone_nearest_azur_port(self.zone),
                                types=('SAFE', 'DANGEROUS'), refresh=False)
            else:
                if self.is_in_globe():
                    self.os_globe_goto_map()
                logger.info('Already at target zone')
                return False
        # MAP_EXIT
        if self.is_in_special_zone():
            self.map_exit()
        # IN_MAP
        if self.is_in_map():
            self.os_map_goto_globe()
        # IN_GLOBE
        # self.ensure_no_zone_pinned()
        self.globe_update()
        self.globe_focus_to(zone)
        if stop_if_safe:
            if self.zone_has_safe():
                logger.info('Zone is safe, stopped')
                self.ensure_no_zone_pinned()
                return False
        self.zone_type_select(types=types)
        self.globe_enter(zone)
        # IN_MAP
        if hasattr(self, 'zone'):
            del self.zone
        self.zone_init()
        # self.map_init()
        return True

    def os_map_goto_globe(self, *args, **kwargs):
        """
        Wraps os_map_goto_globe()
        When zone has uncollected exploration rewards preventing exit,
        run auto search and goto globe again
        """
        for _ in range(3):
            try:
                super().os_map_goto_globe(*args, **kwargs)
                return
            except RewardUncollectedError:
                # Disable after_auto_search since it will exit current zone.
                # Or will cause RecursionError: maximum recursion depth exceeded
                self.run_auto_search(rescan=True, after_auto_search=False)
                continue

        logger.error('Failed to solve uncollected rewards')
        raise GameTooManyClickError

    def port_goto(self, allow_port_arrive=True):
        """
        Wraps `port_goto()`, handle walk_out_of_step

        Returns:
            bool: If success
        """
        for _ in range(3):
            try:
                super().port_goto(allow_port_arrive=allow_port_arrive)
                return True
            except MapWalkError:
                pass

            logger.info('Goto another port then re-enter')
            prev = self.zone
            if prev == self.name_to_zone('NY City'):
                other = self.name_to_zone('Liverpool')
            else:
                other = self.zone_nearest_azur_port(self.zone)
            self.globe_goto(other)
            self.globe_goto(prev)

        logger.warning('Failed to solve MapWalkError when going to port')
        return False

    def fleet_repair(self, revert=True):
        """
        Repair fleets in nearest port.

        Args:
            revert (bool): If go back to previous zone.
        """
        logger.hr('OS fleet repair')
        prev = self.zone
        if self.zone.is_azur_port:
            logger.info('Already in azur port')
        else:
            self.globe_goto(self.zone_nearest_azur_port(self.zone))

        self.port_goto()
        self.port_enter()
        self.port_dock_repair()
        self.port_quit()

        if revert and prev != self.zone:
            self.globe_goto(prev)

    def handle_fleet_repair(self, revert=True):
        """
        Args:
            revert (bool): If go back to previous zone.

        Returns:
            bool: If repaired.
        """
        if self.config.OpsiGeneral_RepairThreshold < 0:
            return False
        if self.is_in_special_zone():
            logger.info('OS is in a special zone type, skip fleet repair')
            return False

        self.hp_get()
        check = [round(data, 2) <= self.config.OpsiGeneral_RepairThreshold if use else False
                 for data, use in zip(self.hp, self.hp_has_ship)]
        if any(check):
            logger.info('At least one ship is below threshold '
                        f'{str(int(self.config.OpsiGeneral_RepairThreshold * 100))}%, '
                        'retreating to nearest azur port for repairs')
            self.fleet_repair(revert=revert)
            self.hp_reset()
            return True
        else:
            logger.info('No ship found to be below threshold '
                        f'{str(int(self.config.OpsiGeneral_RepairThreshold * 100))}%, '
                        'continue OS exploration')
            self.hp_reset()
            return False

    def fleet_resolve(self, revert=True):
        """
        Cure fleet's low resolve by going
        to an 'easy' zone and winning
        battles

        Args:
            revert (bool): If go back to previous zone.
        """
        logger.hr('OS fleet cure low resolve debuff')

        prev = self.zone
        self.globe_goto(22)
        self.zone_init()
        self.run_auto_search()

        if revert and prev != self.zone:
            self.globe_goto(prev)

    def handle_fleet_resolve(self, revert=False):
        """
        Check each fleet if afflicted with the low
        resolve debuff
        If so, handle by completing an easy zone

        Args:
            revert (bool): If go back to previous zone.

        Returns:
            bool:
        """
        if self.is_in_special_zone():
            logger.info('OS is in a special zone type, skip fleet resolve')
            return False

        for index in [1, 2, 3, 4]:
            if not self.fleet_set(index):
                self.device.screenshot()

            if self.fleet_low_resolve_appear():
                logger.info('At least one fleet is afflicted with '
                            'the low resolve debuff')
                self.fleet_resolve(revert)
                return True

        logger.info('None of the fleets are afflicted with '
                    'the low resolve debuff')
        return False

    def handle_current_fleet_resolve(self, revert=False):
        """
        Similar to handle_fleet_resolve,
        but check current fleet only for better performance at initialization

        Args:
            revert (bool): If go back to previous zone.

        Returns:
            bool:
        """
        if self.fleet_low_resolve_appear():
            logger.info('Current fleet is afflicted with '
                        'the low resolve debuff')
            self.fleet_resolve(revert)
            return True

        logger.info('Current fleet is not afflicted with '
                    'the low resolve debuff')
        return False

    def handle_fleet_emp_debuff(self):
        """
        EMP debuff limits fleet step to 1 and messes auto search up.
        It can be solved by moving fleets on map meaninglessly.

        Returns:
            bool: If solved
        """
        if self.is_in_special_zone():
            logger.info('OS is in a special zone type, skip handle_fleet_emp_debuff')
            return False

        def has_emp_debuff():
            return self.appear(FLEET_EMP_DEBUFF, offset=(50, 20))

        for trial in range(5):

            if not has_emp_debuff():
                logger.info('No EMP debuff on current fleet')
                return trial > 0

            current = self.get_fleet_current_index()
            logger.hr(f'Solve EMP debuff on fleet {current}')
            self.globe_goto(self.zone_nearest_azur_port(self.zone))

            logger.info('Find a fleet without EMP debuff')
            for fleet in [1, 2, 3, 4]:
                self.fleet_set(fleet)
                if has_emp_debuff():
                    logger.info(f'Fleet {fleet} is under EMP debuff')
                    continue
                else:
                    logger.info(f'Fleet {fleet} is not under EMP debuff')
                    break

            logger.info('Solve EMP debuff by going somewhere else')
            self.port_goto(allow_port_arrive=False)
            self.fleet_set(current)

        logger.warning('Failed to solve EMP debuff after 5 trial, assume solved')
        return True

    def handle_fog_block(self, repair=True):
        """
        AL game bug where fog remains in OpSi
        even after jumping between zones or
        other pages
        Recover by restarting the game to
        alleviate and resume OpSi task

        Args:
            repair (bool): call handle_fleet_repair after restart
        """
        if not self.appear(MAP_GOTO_GLOBE_FOG):
            return False

        logger.warning(f'Triggered stuck fog status, restarting '
                       f'game to resolve and continue '
                       f'{self.config.task.command}')

        # Restart the game manually rather
        # than through 'task_call'
        # Ongoing task is uninterrupted
        self.device.app_stop()
        self.device.app_start()
        LoginHandler(self.config, self.device).handle_app_login()

        self.ui_ensure(page_os)
        if repair:
            self.handle_fleet_repair(revert=False)

        return True

    def get_action_point_limit(self):
        """
        Override user config at the end of every month.
        To consume all action points without manual configuration.

        Returns:
            int: ActionPointPreserve
        """
        remain = get_os_reset_remain()
        if remain <= 0:
            if self.config.is_task_enabled('OpsiCrossMonth'):
                logger.info('Just less than 1 day to OpSi reset, OpsiCrossMonth is enabled'
                            'set OpsiMeowfficerFarming.ActionPointPreserve to 300 temporarily')
                return 300
            else:
                logger.info('Just less than 1 day to OpSi reset, '
                            'set ActionPointPreserve to 0 temporarily')
                return 0
        elif self.is_cl1_mode_enabled and remain <= 2:
            logger.info('Just less than 3 days to OpSi reset, '
                        'set ActionPointPreserve to 1000 temporarily for hazard 1 leveling')
            return 1000
        elif remain <= 2:
            logger.info('Just less than 3 days to OpSi reset, '
                        'set ActionPointPreserve to 300 temporarily')
            return 300
        else:
            logger.info('Not close to OpSi reset')
            return 2000

    def handle_after_auto_search(self):
        logger.hr('After auto search', level=2)
        solved = False
        solved |= self.handle_fleet_emp_debuff()
        solved |= self.handle_fleet_repair(revert=False)
        logger.info(f'Handle after auto search finished, solved={solved}')
        return solved

    def cl1_ap_preserve(self):
        """
        Keeping enough startup AP to run CL1.
        """
        if self.is_smart_scheduling_enabled:
            return
        if self.is_cl1_enabled and get_os_reset_remain() > 2 \
                and self.get_yellow_coins() > self.config.OS_CL1_YELLOW_COINS_PRESERVE:
            logger.info('Keep 1000 AP when CL1 available')
            if not self.action_point_check(1000):
                self.config.opsi_task_delay(cl1_preserve=True)
                self.config.task_stop()

    _auto_search_battle_count = 0
    _auto_search_round_timer = 0

    def on_auto_search_battle_count_reset(self):
        self._auto_search_battle_count = 0
        self._auto_search_round_timer = 0

    def on_auto_search_battle_count_add(self):
        self._auto_search_battle_count += 1
        logger.attr('battle_count', self._auto_search_battle_count)
        if self.is_in_task_cl1_leveling:
            if self._auto_search_battle_count % 2 == 1:
                if self._auto_search_round_timer:
                    cost = round(time.time() - self._auto_search_round_timer, 2)
                    logger.attr('CL1 time cost', f'{cost}s/round')
                self._auto_search_round_timer = time.time()

    def os_auto_search_daemon(self, drop=None, strategic=False, interrupt=None):
        """
        Args:
            drop (DropRecord):
            strategic (bool): True if running in strategic search
            interrupt (callable | list[callable]): Interrupt callback. A single
                callable means `is_interrupt`; a 2-element list is
                `[is_interrupt, not_interrupt]` for debouncing.

        Returns:
            int: Number of finished battle

        Raises:
            CampaignEnd: If auto search ended
            RequestHumanTakeover: If there's no auto search option.

        Pages:
            in: AUTO_SEARCH_OS_MAP_OPTION_OFF
            out: AUTO_SEARCH_OS_MAP_OPTION_OFF and info_bar_count() >= 2, if no more objects to clear on this map.
                 AUTO_SEARCH_REWARD if get auto search reward.
        """
        logger.hr('OS auto search', level=2)
        self.on_auto_search_battle_count_reset()
        unlock_checked = False
        unlock_check_timer = Timer(5, count=10).start()
        self.ash_popup_canceled = False
        # 侵蚀 1 / 短猫在「奖励出现」时会直接收尾（见 handle_os_auto_search_map_option），
        # 但只有「本次守护循环里确实把自律寻敌打开过」才能这么判：否则奖励可能是上一个
        # 海域延迟弹出来的，仍然需要走原来的重启恢复路径。
        self._os_auto_search_started = False
        confirm_search_start = self.config.task.command in (
            'OpsiHazard1Leveling', 'OpsiMeowfficerFarming')

        def false_func(*args, **kwargs):
            return False

        success = True
        interrupt_confirm = False
        if callable(interrupt):
            is_interrupt, not_interrupt = interrupt, false_func
        elif isinstance(interrupt, list) and len(interrupt) == 2:
            is_interrupt = interrupt[0] if callable(interrupt[0]) else false_func
            not_interrupt = interrupt[1] if callable(interrupt[1]) else false_func
        else:
            is_interrupt, not_interrupt = false_func, false_func
        finished_combat = 0
        died_timer = Timer(1.5, count=3)
        self.hp_reset()
        for _ in self.loop():
            # End
            if not unlock_checked and unlock_check_timer.reached():
                logger.critical('Unable to use auto search in current zone')
                logger.critical('Please finish the story mode of OpSi to unlock auto search '
                                'before using any OpSi functions')
                raise RequestHumanTakeover
            if self.is_in_map():
                self.device.stuck_record_clear()
                if not success:
                    if died_timer.reached():
                        logger.warning('Fleet died confirm')
                        break
                else:
                    if not interrupt_confirm and is_interrupt():
                        interrupt_confirm = True
                    if interrupt_confirm and not_interrupt():
                        interrupt_confirm = False
                    died_timer.reset()
            else:
                died_timer.reset()

            if not unlock_checked:
                if self.appear(AUTO_SEARCH_OS_MAP_OPTION_OFF, offset=(5, 120)):
                    unlock_checked = True
                elif self.appear(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED, offset=(5, 120)):
                    unlock_checked = True
                elif self.appear(AUTO_SEARCH_OS_MAP_OPTION_ON, offset=(5, 120)):
                    unlock_checked = True

            if confirm_search_start and not self._os_auto_search_started \
                    and self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_ON, offset=(5, 120)):
                # 只把「本次守护循环已确认开启」之后的奖励当作正常收尾
                self._os_auto_search_started = True

            if self.handle_os_auto_search_map_option(
                    drop=drop,
                    enable=success
            ):
                unlock_checked = True
                continue
            if self.handle_retirement():
                # Retire will interrupt auto search, need a retry
                self.ash_popup_canceled = True
                continue
            if self.combat_appear():
                self.on_auto_search_battle_count_add()
                if strategic and self.config.task_switched():
                    self.interrupt_auto_search()
                if interrupt_confirm:
                    self.interrupt_auto_search(goto_main=False)
                result = self.auto_search_combat(drop=drop)
                if result:
                    finished_combat += 1
                else:
                    self.hp_get()
                    if any(self.need_repair):
                        success = False
                        logger.warning('Fleet died, stop auto search')
                        continue
            if self.handle_map_event():
                # Auto search can not handle siren searching device.
                continue

        return finished_combat

    def interrupt_auto_search(self, goto_main=True):
        """
        Args:
            goto_main (bool): If go to main page. Set False to stop auto search
                while staying in the map (used by keep-mission-zone).

        Raises:
            TaskEnd: If auto search interrupted

        Pages:
            in: Any, usually to be is_combat_executing
            out: page_main or IN_MAP
        """
        logger.info('Interrupting auto search')
        is_loading = False
        pause_interval = Timer(0.5, count=1)
        in_main_timer = Timer(3, count=6)
        in_map_timer = Timer(1, count=6)
        for _ in self.loop():
            # End
            if self.is_in_main():
                logger.info('Auto search interrupted')
                self.config.task_stop()
            if not goto_main and self.is_in_map() and in_map_timer.reached():
                logger.info('Auto search interrupted (stay in map)')
                self.config.task_stop()

            if self.appear_then_click(AUTO_SEARCH_REWARD, offset=(50, 50), interval=3):
                self.interval_clear(GOTO_MAIN)
                in_main_timer.reset()
                in_map_timer.reset()
                continue
            if pause_interval.reached():
                pause = self.is_combat_executing()
                if pause:
                    self.device.click(pause)
                    self.interval_reset(MAINTENANCE_ANNOUNCE)
                    is_loading = False
                    pause_interval.reset()
                    in_main_timer.reset()
                    in_map_timer.reset()
                    continue
            if self.handle_combat_quit():
                self.interval_reset(MAINTENANCE_ANNOUNCE)
                pause_interval.reset()
                in_main_timer.reset()
                in_map_timer.reset()
                continue
            if self.handle_combat_quit_reconfirm():
                self.interval_reset(MAINTENANCE_ANNOUNCE)
                pause_interval.reset()
                in_main_timer.reset()
                in_map_timer.reset()
                continue

            if goto_main and self.appear_then_click(GOTO_MAIN, offset=(20, 20), interval=3):
                in_main_timer.reset()
                continue
            if self.ui_additional():
                continue
            if self.handle_map_event():
                continue
            # Only print once when detected
            if not is_loading:
                if self.is_combat_loading():
                    is_loading = True
                    in_main_timer.clear()
                    in_map_timer.clear()
                    continue
                # Random background from page_main may trigger EXP_INFO_*, don't check them
                if in_main_timer.reached():
                    logger.info('handle_exp_info')
                    if self.handle_battle_status():
                        continue
                    if self.handle_exp_info():
                        continue
            elif self.is_combat_executing():
                is_loading = False
                in_main_timer.clear()
                in_map_timer.clear()
                continue

    def os_auto_search_run(self, drop=None, strategic=False, interrupt=None):
        """
        Args:
            drop (DropRecord):
            strategic (bool): True to use strategic search
            interrupt (callable | list[callable]): Interrupt callback for auto search.

        Returns:
            int: Number of finished combat
        """
        finished_combat = 0
        for _ in range(5):
            backup = self.config.temporary(Campaign_UseAutoSearch=True)
            try:
                if strategic:
                    self.strategic_search_start()
                combat = self.os_auto_search_daemon(drop=drop, strategic=strategic, interrupt=interrupt)
                finished_combat += combat
            except CampaignEnd:
                finished_combat += self._auto_search_battle_count
                logger.info('OS auto search finished')
            finally:
                backup.recover()

            # Continue if was Auto search interrupted by ash popup
            # Break if zone cleared
            if self.config.is_task_enabled('OpsiAshBeacon'):
                if self.handle_ash_beacon_attack() or self.ash_popup_canceled:
                    strategic = False
                    continue
                else:
                    break
            else:
                if self.info_bar_count() >= 2:
                    break
                elif self.ash_popup_canceled:
                    continue
                else:
                    break

        return finished_combat

    def clear_question(self, drop=None):
        """
        Clear nearly (and 3 grids from above) question marks on radar.
        Try 3 times at max to avoid loop tries on 2 adjacent fleet mechanism.

        `_question_unreachable` semantics follow AzurPilot: it is set ONLY when every
        attempt saw a question but none could be cleared. Any other outcome -- e.g. a
        question that turned out to be a plain story event and got consumed -- must
        NOT mark it, otherwise the caller moves fleets to hunt a question that no
        longer exists.

        Returns:
            bool: If cleared
        """
        logger.hr('Clear question', level=2)
        self._question_unreachable = False
        question_seen = False
        for _ in range(3):
            grid = self.radar.predict_question(self.device.image, in_port=self.zone.is_port)
            if grid is None:
                logger.info('No question mark above current fleet on this radar')
                return False

            question_seen = True
            logger.info(f'Found question mark on {grid}')
            self.handle_info_bar()

            self.update_os()
            self.view.predict()
            self.view.show()

            # Camera drift (no current fleet in the local view, e.g. after a full
            # rescan left the camera at a scan position): a fallback conversion would
            # use the camera center as the fleet position and click a wrong grid.
            if self.view.select(is_current_fleet=True).count == 0:
                self._os_camera_recover_to_fleet()

            try:
                grid = self.convert_radar_to_local(grid)
            except KeyError:
                # The question sits outside the local view (e.g. the fleet is at the
                # map edge and the question is one grid beyond it). Recover the
                # camera when the fleet is not visible either, then retry -- do NOT
                # mark it unreachable here (AzurPilot retries instead).
                if self.view.select(is_current_fleet=True).count == 0:
                    self._os_camera_recover_to_fleet()
                else:
                    logger.warning('Question mark is outside the local view, skip this grid')
                continue
            self.is_siren_device_confirmed = False
            self.device.click(grid)
            with self.config.temporary(STORY_ALLOW_SKIP=False):
                result = self.wait_until_walk_stable(
                    drop=drop, walk_out_of_step=False, confirm_timer=Timer(1.5, count=4))
            if 'akashi' in result:
                self._solved_map_event.add('is_akashi')
                return True
            elif 'event' in result and grid.is_logging_tower:
                self._solved_map_event.add('is_logging_tower')
                return True
            elif 'event' in result and (grid.is_scanning_device or self.is_siren_device_confirmed):
                # 问号点过去开出的是塞壬装置：剧情选项已由 story_skip 的
                # `_identify_siren_device_option` 处理完成（信息收集装置/柱子
                # 点中间项即完成并给奖励）。标记为已解决并停止巡逻——对齐
                # AP master（map.py:1322-1406）：collected 无需自律寻敌。
                logger.hr('Siren device solved via question', level=2)
                siren_mode = getattr(self, 'siren_device_mode', None)
                logger.attr('Siren device mode', siren_mode)
                if siren_mode == 'enemy':
                    task = self.config.task.command
                    if task not in ('OpsiHazard1Leveling', 'OpsiMeowfficerFarming'):
                        task = 'OpsiHazard1Leveling'
                    siren_fleet = self.config.cross_get(
                        keys=f'{task}.OpsiSirenBug.Siren_Fleet', default=0)
                    current_fleet = self.fleet_selector.get()
                    if siren_fleet > 0:
                        self.fleet_set(siren_fleet)
                    for _ in range(3):
                        self.os_auto_search_run(drop=drop)
                    if siren_fleet > 0:
                        self.fleet_set(current_fleet)
                elif siren_mode == 'collected':
                    logger.info('Siren info collection device: dialog already completed, '
                                'no auto search needed')
                else:
                    logger.info('Siren device standard handling, run auto search')
                    self.os_auto_search_run(drop=drop)
                self._solved_map_event.add('is_scanning_device')
                return True
            else:
                logger.info(f'Question turned into {result}, expected: {grid.str}, re-predict radar')
                continue

        logger.warning('Failed to goto question mark after 3 trail, '
                       'this might be 2 adjacent fleet mechanism, stopped')
        self._question_unreachable = question_seen
        return False

    def clear_question_any_fleet(self):
        """
        L0/L1 of fixed patrol: read fleet 1-4 radars, clearing question marks
        without moving any fleet to a patrol landing point.

        AzurPilot order per fleet: predict the radar first -- only run the
        clear-question flow when this fleet really sees a question mark -- then,
        after clearing a plain question, immediately rescan the whole map to
        pick up events that were hidden by the question / blocking fleet. A
        target event found that way stops the patrol here, so the (much more
        expensive) L2 fleet movement is never reached.

        Returns:
            bool: True if a target event was found and solved.
        """
        primary = self.config.OpsiFleet_Fleet
        solved_events = set()
        question_unreachable = False
        try:
            for fleet in [primary] + [index for index in (1, 2, 3, 4) if index != primary]:
                logger.info(f'[FIXED PATROL][L0] Check Fleet {fleet}')
                if not self._set_fixed_patrol_fleet(fleet):
                    continue

                self.device.screenshot()
                question = self.radar.predict_question(
                    self.device.image, in_port=self.zone.is_port)
                if question is None:
                    logger.info(f'[FIXED PATROL][L0] Fleet {fleet}: radar has no question')
                    continue

                self._solved_map_event = set()
                self._solved_fleet_mechanism = False
                self.clear_question()
                solved_events.update(self._solved_map_event)
                question_unreachable |= self._question_unreachable
                if solved_events & ALREADY_SOLVED_MAP_EVENTS:
                    logger.info(
                        f'[FIXED PATROL][L0] Fleet {fleet} solved an event, stop'
                    )
                    return True

                # Clearing a plain question can reveal an event somewhere else on
                # the map (the question or the blocking fleet was hiding it). Do a
                # full map scan before switching to the next fleet, so L0 can still
                # stop without moving any fleet (AzurPilot).
                self._solved_map_event = set()
                self._solved_fleet_mechanism = False
                try:
                    self.map_rescan_once(rescan_mode='full')
                except UNSWALLOWABLE_ERRORS:
                    raise
                except Exception as e:
                    logger.debug(
                        f'Fixed patrol L0: rescan after clearing a question failed, '
                        f'continue: {e}', exc_info=True)
                solved_events.update(self._solved_map_event)
                if self._solved_map_event & ALREADY_SOLVED_MAP_EVENTS:
                    logger.info(
                        f'[FIXED PATROL][L0] Event revealed after clearing a question, stop'
                    )
                    return True
                logger.info(f'[FIXED PATROL][L0] Fleet {fleet}: no actionable event')
        finally:
            self._solved_map_event = solved_events
            self._question_unreachable = question_unreachable
            self.fleet_set(primary)
        logger.info('[FIXED PATROL][L0] All fleets checked')
        return False

    def _read_current_action_point(self):
        self.action_point_enter()
        try:
            self.action_point_safe_get()
            return int(self._action_point_current)
        finally:
            self.action_point_quit()

    def _set_fixed_patrol_fleet(self, fleet):
        self.fleet_set(fleet)
        current = self.fleet_selector.get()
        if current != fleet:
            logger.warning(
                f'Fixed patrol expected fleet {fleet}, but current fleet is {current}; skip it'
            )
            return False
        return True

    def _fixed_patrol_candidate_grids(self, target_loc, occupied_locations=None):
        """
        Generate candidate landing grids for fixed patrol.

        The target grid may be out of the fleet's move range, so nearby empty sea
        grids are used as fallbacks. A fleet which only reached a fallback still
        counts as moved, because the fleet that blocked the event is no longer on
        its original grid, so the following full rescan can find the event again.

        Args:
            target_loc (tuple[int, int]): Target location, (2, 0) is C1.
            occupied_locations (iterable): Locations to avoid.

        Returns:
            list: Candidate grids, the target grid first.
        """
        occupied = set(occupied_locations or [])
        offsets = (
            (0, 0), (0, 1), (0, 2), (-1, 1), (1, 1),
            (-1, 2), (1, 2), (-1, 0), (1, 0), (0, 3),
        )
        # Absolute fallback rows, which are row 12 and 13 on screen.
        absolute_fallback_rows = (11, 12)
        candidates = []
        seen = set()
        locations = [
            (target_loc[0] + dx, target_loc[1] + dy)
            for dx, dy in offsets
        ]
        locations.extend((target_loc[0], row) for row in absolute_fallback_rows)

        for location in locations:
            if location in seen or location not in self.map or location in occupied:
                continue
            seen.add(location)
            grid = self.map[location]
            if (
                grid.is_land
                or grid.is_enemy
                or grid.is_siren
                or grid.is_boss
                or grid.is_fortress
                or getattr(grid, 'is_mechanism_block', False)
                or getattr(grid, 'is_fleet', False)
            ):
                continue
            candidates.append(grid)
        return candidates

    def safe_swipe(self, start, end, duration=0.5, retries=2):
        """
        Swipe with retries.

        Args:
            start (tuple[int, int]): Swipe start point.
            end (tuple[int, int]): Swipe end point.
            duration (float): Swipe duration in seconds.
            retries (int): Max attempts.

        Returns:
            bool: True if any attempt succeeded.
        """
        for attempt in range(1, retries + 1):
            try:
                with suppress(Exception):
                    self.device.stuck_record_clear()
                self.device.swipe(start, end, duration=duration)
                time.sleep(0.45)
                return True
            except Exception as e:
                logger.warning(f'Fixed patrol: safe swipe attempt {attempt} failed: {e}')
                time.sleep(0.4)
        return False

    def _fixed_patrol_soft_recover(self):
        """
        Rebuild map data after a walk or click error, without restarting the game.

        Returns:
            bool: True if the map view is usable again.
        """
        logger.info('Fixed patrol: soft recover, screenshot and rebuild view')
        self.device.screenshot()
        try:
            self.ui_ensure(page_os)
            self.map_init(map_=None)
            self.update()
            return True
        except UNSWALLOWABLE_ERRORS:
            # `ui_ensure()` raises GamePageUnknownError/GameNotRunningError and the task layer
            # raises TaskEnd. Swallowing them here means "recovery failed but the screen is
            # fine", so the caller keeps clicking candidate grids on a screen that is not the
            # map -- the failure that ended a 2026-10-04 run.
            raise
        except Exception:
            logger.debug('Fixed patrol: soft recover failed', exc_info=True)
            return False

    def _fixed_patrol_app_restart(self):
        """
        Last resort of fixed patrol: restart the game and rebuild map data.

        Returns:
            bool: True if the map view is usable again.
        """
        logger.warning('Fixed patrol: restarting game to recover')
        try:
            self.device.app_stop()
            time.sleep(1.0)
            self.device.app_start()
            LoginHandler(self.config, self.device).handle_app_login()
            self.ui_ensure(page_os)
            time.sleep(0.8)
            self.map_init(map_=None)
            self.update()
            return True
        except UNSWALLOWABLE_ERRORS:
            # `handle_app_login()` raises RequestHumanTakeover when the game will not come up
            # (maintenance, login timeout, a popup it cannot clear). Those need the task
            # layer's handling -- error log, push, controlled stop -- not a local
            # "recovery failed, carry on clicking" return.
            raise
        except Exception:
            logger.error('Fixed patrol: restarting game failed', exc_info=True)
            return False

    def _try_fixed_patrol_move(self, fleet, target_grid, primary_target):
        """
        Move a fleet to one candidate grid by clicking it.

        Fixed patrol only needs the fleet to leave its current grid, so the grid
        is clicked directly instead of walking a planned path. This avoids the 20s
        walk timeout, the camera rebuild and the repeated clicks on the same grid
        that happen when path walking cannot reach the destination.

        Args:
            fleet (int): Fleet index, 1-4.
            target_grid (Grid): Candidate grid to move to.
            primary_target (tuple[int, int]): Target location of this fleet.

        Returns:
            bool: True if the fleet arrived.
        """
        try:
            self.focus_to(target_grid.location)
            self.update()
            clickable_grid = self.convert_global_to_local(target_grid.location)
        except KeyError:
            logger.warning(
                f'Fixed patrol: focused {location2node(target_grid.location)}, '
                f'but no clickable grid in sight'
            )
            return False
        except MapDetectionError:
            # The map view is broken. Rebuilding it here is cheaper than letting the
            # error reach the scheduler, which would restart the whole game.
            logger.warning('Fixed patrol: map detection failed while focusing, skip candidate')
            self._fixed_patrol_soft_recover()
            return False

        for try_index in range(2):
            try:
                with suppress(Exception):
                    self.device.stuck_record_clear()
                time.sleep(0.1)
                self.device.click(clickable_grid)
                self.wait_until_walk_stable(confirm_timer=Timer(1.5, count=4))
                if target_grid.location == primary_target:
                    logger.info(
                        f'Fixed patrol: fleet {fleet} arrived '
                        f'{location2node(target_grid.location)}'
                    )
                else:
                    logger.info(
                        f'Fixed patrol: fleet {fleet} cannot reach '
                        f'{location2node(primary_target)}, '
                        f'stopped at {location2node(target_grid.location)}'
                    )
                return True
            except MapWalkError as error:
                if str(error) == 'walk_out_of_step':
                    logger.warning(
                        f'Fixed patrol: fleet {fleet} moving to '
                        f'{location2node(target_grid.location)} is out of range, '
                        f'try another candidate'
                    )
                    return False
                logger.warning(
                    f'Fixed patrol: fleet {fleet} walk error: {error} ({try_index + 1}/2)'
                )
            except GameTooManyClickError as error:
                logger.warning(
                    f'Fixed patrol: fleet {fleet} click error: {error} ({try_index + 1}/2)'
                )

            if try_index == 0:
                recovered = self._fixed_patrol_soft_recover()
            else:
                recovered = self._fixed_patrol_app_restart()
            if not recovered:
                return False
            # The recovery may have changed the current fleet, ensure it again.
            self.fleet_set(fleet)
            # Recovery leaves the camera wherever the game parked it. Converting the
            # target without re-focusing raises KeyError whenever the target is out of
            # sight, which throws away a candidate that is actually reachable.
            try:
                self.focus_to(target_grid.location)
                self.update()
                clickable_grid = self.convert_global_to_local(target_grid.location)
            except (KeyError, MapDetectionError):
                logger.warning(
                    f'Fixed patrol: fleet {fleet} lost '
                    f'{location2node(target_grid.location)} after recovery'
                )
                return False
            time.sleep(0.5)

        return False

    def _move_fleet_to_patrol(self, fleet, target_loc):
        """
        Force a fleet to leave its current grid.

        Tries the target grid first, then nearby empty grids. The fleet counts as
        moved as long as it left its original grid, because the fleet that blocked
        the event is gone, and the following full rescan can find the event again.

        Args:
            fleet (int): Fleet index, 1-4.
            target_loc (tuple[int, int]): Target location, (2, 0) is C1.

        Returns:
            bool: True if the fleet left its original grid.
        """
        target_grid_group = self.map.select(location=target_loc)
        if not target_grid_group:
            logger.warning(f'Fixed patrol: grid {target_loc} not found, skip fleet {fleet}')
            return False
        target_grid = target_grid_group[0]

        logger.hr(
            f'Fixed patrol: move fleet {fleet} to {location2node(target_grid.location)}', level=2)
        if not self._set_fixed_patrol_fleet(fleet):
            return False

        # Reset the camera to a known corner first, so that the following
        # focus_to() does not start from a stale camera position.
        logger.info('Fixed patrol: reset camera position')
        top_point = (640, 150)
        bottom_point = (640, 600)
        quick_ok = True
        try:
            for _ in range(2):
                self.device.swipe(top_point, bottom_point, duration=0.3)
                time.sleep(0.18)
        except Exception:
            quick_ok = False
            logger.debug('Fixed patrol: quick swipe reset failed, trying safe swipe')
        if not quick_ok and not self.safe_swipe(top_point, bottom_point, duration=0.55, retries=2):
            logger.warning('Fixed patrol: camera reset failed, continue anyway')
        time.sleep(0.45)

        candidate_grids = self._fixed_patrol_candidate_grids(target_loc)
        if not candidate_grids:
            logger.warning(
                f'Fixed patrol: fleet {fleet} has no available landing grid near '
                f'{location2node(target_loc)}'
            )
            return False

        moved = False
        fallback_location = None
        for candidate_index, candidate_grid in enumerate(candidate_grids[:4]):
            if candidate_index > 0:
                logger.info(
                    f'Fixed patrol: fleet {fleet} try fallback '
                    f'{location2node(candidate_grid.location)} '
                    f'(target {location2node(target_loc)})'
                )
            if self._try_fixed_patrol_move(fleet, candidate_grid, target_loc):
                if candidate_grid.location == target_loc:
                    moved = True
                    break

                fallback_location = candidate_grid.location
                logger.info(
                    f'Fixed patrol: fleet {fleet} stopped at fallback '
                    f'{location2node(candidate_grid.location)}, '
                    f'trying the real target {location2node(target_loc)}'
                )
                if self._try_fixed_patrol_move(fleet, target_grid, target_loc):
                    moved = True
                    logger.info(
                        f'Fixed patrol: fleet {fleet} returned to the real target '
                        f'{location2node(target_loc)}'
                    )
                    break

                logger.warning(
                    f'Fixed patrol: fleet {fleet} cannot return to '
                    f'{location2node(target_loc)} from '
                    f'{location2node(candidate_grid.location)}, try another candidate'
                )

        if not moved:
            if fallback_location is not None:
                logger.info(
                    f'Fixed patrol: fleet {fleet} cannot return to '
                    f'{location2node(target_loc)}, stays at fallback '
                    f'{location2node(fallback_location)}'
                )
                moved = True
            else:
                logger.warning(
                    f'Fixed patrol: fleet {fleet} failed on {location2node(target_loc)} '
                    f'and all of its fallbacks'
                )

        return moved

    def _move_fleets_and_rescan(self):
        """
        L2 of fixed patrol: move fleets away one by one and rescan the whole map.

        Fleets are handled as "primary fleet first, then the rest in index order".
        Each fleet is moved to its landing column (1 -> C1, 2 -> D1, 3 -> E1,
        4 -> F1) to get a blocking fleet out of the way, then the whole map is
        rescanned. A radar pre-check runs before each move; the move stops as soon
        as an event is solved, so it never blindly cycles all four fleets.

        Returns:
            bool: True if an event was found and solved, False otherwise.
        """
        primary = self.config.OpsiFleet_Fleet
        columns = {1: (2, 0), 2: (3, 0), 3: (4, 0), 4: (5, 0)}  # C1, D1, E1, F1
        order = [primary] + [fleet for fleet in (1, 2, 3, 4) if fleet != primary]
        backup = self.config.temporary(
            OpsiGeneral_RepairThreshold=-1, Campaign_UseAutoSearch=False)
        try:
            for fleet in order:
                # Radar pre-check: if the current fleet can solve an event nearby,
                # do it now instead of moving this fleet.
                logger.info(f'[FIXED PATROL][L2] Fleet {fleet} radar pre-check')
                if not self._set_fixed_patrol_fleet(fleet):
                    continue
                self._solved_map_event = set()
                self._solved_fleet_mechanism = False
                self.clear_question(drop=None)
                if self._solved_map_event & ALREADY_SOLVED_MAP_EVENTS:
                    logger.info('[FIXED PATROL][L2] Event solved during radar pre-check, stop')
                    return True
                logger.info('[FIXED PATROL][L2] Radar pre-check: no actionable event')

                if not self._move_fleet_to_patrol(fleet, columns[fleet]):
                    continue

                # The blocking fleet is away, rescan the whole map to find events.
                self._solved_map_event = set()
                self._solved_fleet_mechanism = False
                logger.info(f'[FIXED PATROL][L2] FULL RESCAN after Fleet {fleet} movement')
                try:
                    self.map_rescan(rescan_mode='full')
                except UNSWALLOWABLE_ERRORS:
                    raise
                except Exception as e:
                    logger.debug(
                        f'Fixed patrol L2: rescan after moving failed, continue: {e}', exc_info=True)

                if self._solved_map_event & ALREADY_SOLVED_MAP_EVENTS:
                    logger.info(
                        f'[FIXED PATROL][L2] Rescan result: event found '
                        f'{sorted(self._solved_map_event & ALREADY_SOLVED_MAP_EVENTS)}'
                    )
                    logger.info('[FIXED PATROL] Stop fixed patrol immediately')
                    return True
                logger.info('[FIXED PATROL][L2] Rescan result: no actionable event')
        finally:
            backup.recover()
        logger.info('[FIXED PATROL] L2 finished: result=NO_EVENT')
        return False

    def _is_meowfficer_task(self) -> bool:
        """
        Whether the current run is 短猫相接.

        Also true while OpsiScheduling proxies it: the proxied sub-task carries its
        own identity in the task context, so the check must not read
        `config.task.command` alone (see task_context).
        """
        context = current_opsi_context(self.config)
        current = getattr(context, 'current_task', '') if context is not None else ''
        task = getattr(self.config, 'task', None)
        return (current or str(getattr(task, 'command', ''))) == 'OpsiMeowfficerFarming'

    # Event grids that were already judged unreachable in the current rescan round.
    _unreachable_event_nodes = set()

    def _os_camera_recover_to_fleet(self, fleet=None):
        """
        Re-focus the camera on the current fleet by switching fleets and back.

        The game sometimes leaves the camera elsewhere (after auto search / event
        handling); the local view then has no current fleet, radar coordinates can not
        be converted into clickable grids, and visible events are wrongly treated as
        out of reach. `fleet_set` already waits for the camera to settle.

        Args:
            fleet (int | None): Fleet to focus, defaults to the current one.

        Returns:
            bool: True if the current fleet is visible again.
        """
        if fleet is None:
            fleet = self.fleet_selector.get()
        logger.warning(f'Camera is not following fleet {fleet}, switch fleets to re-focus')
        other = 1 if fleet != 1 else 2
        self.fleet_set(other)
        self.fleet_set(fleet)
        self.device.screenshot()
        self.update_os()
        self.view.predict()
        if self.view.select(is_current_fleet=True).count == 1:
            logger.info('Camera re-focused on the current fleet')
            return True
        logger.warning('Camera still not following after the re-focus, view detection may be broken')
        return False

    def _mark_event_unreachable(self, node):
        """
        Remember an event grid that cannot be reached in this rescan round.

        Rebind instead of in-place `add`: a class-level default set is shared by
        every instance, and an in-place add would leak the marker into others.

        Args:
            node (str): Event grid, e.g. 'B7'.
        """
        self._unreachable_event_nodes = set(self._unreachable_event_nodes) | {node}

    def _radar_question_to_local(self):
        """
        Locate the white question mark on the current fleet's radar as a local grid.

        Akashi / logging tower / scanning device icons are often hidden behind the
        fleet model, so the view detection flickers. The radar question mark is the
        same detection `clear_question` uses and is steadier.

        Returns:
            Grid or None: Local grid to click, None if the radar shows no question.
        """
        question = self.radar.predict_question(self.device.image, in_port=self.zone.is_port)
        if question is None:
            return None
        try:
            return self.convert_radar_to_local(question)
        except KeyError:
            return None

    def _goto_akashi_with_other_fleets(self, drop=None):
        """
        Akashi is visible but the current fleet cannot reach it: try the other fleets.

        Walking into Akashi usually fails because an idle fleet blocks the path or the
        area movement count is used up. Any fleet can buy from the shop, so switch
        through the rest: buy directly when one is adjacent, otherwise click Akashi and
        let it walk. The first success wins; when all fail the original fleet is
        restored and the caller falls back to the fixed patrol.

        Args:
            drop: Drop record.

        Returns:
            bool: True if a fleet managed to buy from Akashi.
        """
        current = self.fleet_selector.get()
        logger.info(f'Current fleet {current} cannot reach Akashi, try other fleets')
        try:
            for fleet in [f for f in [1, 2, 3, 4] if f != current]:
                self.fleet_set(fleet)
                self.device.screenshot()
                self.update_os()
                self.view.predict()
                grids = self.view.select(is_akashi=True)
                if grids and grids[0].is_akashi:
                    grid = grids[0]
                else:
                    grid = self._radar_question_to_local()
                    if grid is None:
                        logger.info(f'Fleet {fleet} has no Akashi in sight, next')
                        continue
                    logger.info(f'Fleet {fleet} did not identify Akashi, fallback to the radar question')
                fleet_loc = self.convert_radar_to_local((0, 0))
                # "Adjacent, buy directly" only when the view really says it is Akashi:
                # a radar question can also be a logging tower / device, which has to be
                # clicked and walked to instead of being opened as a shop.
                if grid.is_akashi and fleet_loc.distance_to(grid) <= 1:
                    logger.info(f'Akashi ({grid}) is near fleet {fleet} ({fleet_loc}), buy directly')
                    self.handle_akashi_supply_buy(grid)
                    self._solved_map_event.add('is_akashi')
                    return True
                logger.info(f'Fleet {fleet} clicks Akashi ({grid}) and tries to walk there')
                self.device.click(grid)
                with self.config.temporary(STORY_ALLOW_SKIP=False):
                    walk_time = 1.5 + 0.6 * grid.distance_to(fleet_loc)
                    result = self.wait_until_walk_stable(
                        confirm_timer=Timer(walk_time, count=4),
                        drop=drop,
                        walk_out_of_step=False,
                    )
                if 'akashi' in result:
                    self._solved_map_event.add('is_akashi')
                    return True
                logger.info(f'Fleet {fleet} cannot reach Akashi either, next')
            return False
        finally:
            # Restore the original fleet on every path, so later steps do not act on
            # a wrong fleet.
            self.fleet_set(current)

    def _goto_scanning_device_with_other_fleets(self, drop=None):
        """
        The current fleet cannot reach the siren device: try the other fleets.

        Walking into the device usually fails because an idle fleet blocks the path.
        Any fleet can click the device and open the dialog, so switch through the rest
        and click; the first fleet that reaches it wins. When all fail the original
        fleet is restored and the caller falls back to the fixed patrol.

        Args:
            drop: Drop record.

        Returns:
            bool: True if a fleet reached the device and opened its dialog.
        """
        current = self.fleet_selector.get()
        logger.info(f'Current fleet {current} cannot reach the siren device, try other fleets')
        try:
            for fleet in [f for f in [1, 2, 3, 4] if f != current]:
                self.fleet_set(fleet)
                self.device.screenshot()
                self.update_os()
                self.view.predict()
                grids = self.view.select(is_scanning_device=True)
                if grids and grids[0].is_scanning_device:
                    grid = grids[0]
                else:
                    grid = self._radar_question_to_local()
                    if grid is None:
                        logger.info(f'Fleet {fleet} has no device in sight, next')
                        continue
                    logger.info(f'Fleet {fleet} did not identify the device, fallback to the radar question')
                logger.info(f'Fleet {fleet} clicks the device ({grid}) and tries to walk there')
                self.device.click(grid)
                with self.config.temporary(STORY_ALLOW_SKIP=False):
                    result = self.wait_until_walk_stable(
                        drop=drop, walk_out_of_step=False, confirm_timer=Timer(3, count=4))
                if 'event' in result:
                    logger.info(f'Fleet {fleet} reached the siren device')
                    self._set_device_state(DEVICE_DIALOG_OPEN)
                    return True
                logger.info(f'Fleet {fleet} cannot reach the device either, next')
            return False
        finally:
            # Restore the original fleet on every path, so later steps do not act on
            # a wrong fleet.
            self.fleet_set(current)

    def _recover_unreachable_akashi(self, drop, node):
        """
        Unified fallback when Akashi cannot be reached: other fleets first, then the
        fixed patrol.

        Akashi's icon is hidden by the fleet model and the view detection flickers, so
        both failing paths (bought nothing, or no longer identified) share this.

        Args:
            drop: Drop record.
            node (str): Akashi grid, e.g. 'B7'. A full rescan sees the same grid from
                several camera views; this keeps the slow path to once per round.

        Returns:
            bool: True if some fleet bought from Akashi.
        """
        if self._is_meowfficer_task():
            # 短猫相接不走这套共享兜底（换队点明石 + 挪舰队），它只有换队扫雷达。
            logger.info('Meowfficer farming does not use the shared Akashi fallback, '
                        'its own radar patrol handles it')
            return False
        if node in self._unreachable_event_nodes:
            logger.info(f'Akashi on {node} was already judged unreachable this round, skip')
            return False
        if self._goto_akashi_with_other_fleets(drop=drop):
            return True
        logger.info('No fleet can reach Akashi, run the fixed patrol')
        self._mark_event_unreachable(node)
        self.execute_fixed_patrol_scan()
        return False

    def _forced_move_enabled(self):
        """
        Read the fixed patrol switch, tolerating the legacy level numbers.

        Returns:
            bool: True if fixed patrol is enabled.
        """
        value = getattr(self.config, 'OpsiHazard1Leveling_ExecuteFixedPatrolScan', None)
        if value is None:
            # The fallbacks run from the shared rescan code too, where 侵蚀1 may not
            # be bound; read the task's config directly in that case.
            value = self.config.cross_get(
                'OpsiHazard1Leveling.OpsiHazard1Leveling.ExecuteFixedPatrolScan', default=False)
        if isinstance(value, str):
            value = value.strip().lower()
            if value in ('true', '1', '2', '3'):
                return True
            if value in ('false', '0', ''):
                return False
            return bool(value)
        if isinstance(value, bool):
            return value
        # Legacy level config: 0 is off, 1 is the efficiency mode and 2 is the
        # conservative mode. The conservative mode was merged into the efficiency
        # mode, so every level above 0 counts as enabled.
        try:
            return int(value) > 0
        except (TypeError, ValueError):
            return bool(value)

    def _meowfficer_patrol_enabled(self):
        """
        Read `OpsiMeowfficerFarming.ExecuteFixedPatrolScan`, tolerating a missing key.
        """
        value = getattr(self.config, 'OpsiMeowfficerFarming_ExecuteFixedPatrolScan', None)
        if value is None:
            value = self.config.cross_get(
                'OpsiMeowfficerFarming.OpsiMeowfficerFarming.ExecuteFixedPatrolScan', default=False)
        return bool(value)

    def _primary_radar_swept_later(self):
        """
        Whether the per-fleet radar sweep of this same round already covers the primary
        fleet, so clearing its question marks before the rescan would read that radar twice.

        Both patrols start with the primary fleet, so with a patrol running this round the
        clear is done there instead. AzurPilot removed its own step-by-step chain for
        exactly this reason (d784de8fc: reading radars is the fixed patrol's job), but it
        gave up the clear entirely when the patrol switch is off -- we keep it there, since
        that read is what finds an akashi or a device hiding behind a question mark.

        Returns:
            bool: True when the sweep happens later in the same round anyway.
        """
        if self._is_meowfficer_task():
            return self._meowfficer_patrol_enabled()
        return self._forced_move_enabled()

    _in_forced_recovery = False

    def execute_fixed_patrol_scan(self):
        """
        Run fixed patrol and rescan the map.

        After the auto search of hazard level 1, when a normal rescan finds nothing
        (usually akashi is hidden behind a fleet, or spawned out of radar range),
        fixed patrol searches in two stages:

        L0/L1 (zero movement): read the radars of fleet 1-4, switching fleets only
                and moving none of them, which is the fastest way to find akashi,
                logging towers and scanning devices. It stops on the first hit.
        L2 (move fleets): move fleets one by one, rescanning the whole map after
                each move, to get the blocking fleet out of the way. Whether to
                move depends on the situation:
                ① a fleet saw a question mark but cannot reach it (blocked by
                   another fleet, or out of move range): must move, and it does not
                   depend on action points, because a seen event should not be
                   given up;
                ② no clue on any radar: only move when the current action point shown
                   on the UI is above `_FIXED_PATROL_L2_AP`, otherwise leave it to
                   the next farming round.

        Returns:
            bool: True if an event was found and solved.
        """
        if not self._forced_move_enabled():
            logger.info('Fixed patrol: switch is off, skipped')
            return False
        if self._is_meowfficer_task():
            # 短猫相接不走这套共享强制移动（与 AP master 一致）：L2 把舰队挪到的
            # C1/D1/E1/F1 是照侵蚀1 那张图定的，短猫跑的海域各不相同，挪了没意义
            # 还可能把舰队挪到不该去的地方。短猫的强制移动只有换队扫雷达。
            logger.info('Fixed patrol: meowfficer farming uses its own radar-only patrol, '
                        'skip the shared L2')
            return False
        # Guard against recursive re-entry: if the event handler already triggered
        # fixed patrol (e.g. Akashi unreachable), don't nest another full patrol.
        if self._in_forced_recovery:
            logger.info('Fixed patrol: already in forced recovery, skip nested call')
            return False

        self.map_init(map_=None)
        if not self.map.grids:
            logger.warning('Fixed patrol: no map grids detected, skipped')
            return False

        self._in_forced_recovery = True
        try:
            # ---- L0/L1: switch fleets to read the radar, move nothing ----
            logger.hr('Fixed patrol: read radar with all fleets, without moving')
            self._solved_map_event = set()
            self._solved_fleet_mechanism = False
            if self.clear_question_any_fleet():
                return True

            # ---- L2: move fleets ----
            # "Saw it but cannot reach it" can only be solved by moving a fleet and
            # has nothing to do with action points. Only the "no clue at all" case
            # checks action points, because this round is an extra round opened for
            # hunting events, and a low action point is left to the next farming round.
            if self._question_unreachable:
                logger.info(
                    'Fixed patrol: a fleet saw a question mark but cannot reach it, '
                    'going to L2'
                )
            else:
                current_ap = self._read_current_action_point()
                if not should_move_fleet_for_fixed_patrol(current_ap, False):
                    logger.info(
                        f'Fixed patrol: no event seen and current AP {current_ap} is not above '
                        f'{self._FIXED_PATROL_L2_AP}, skipped, left to the next farming round'
                    )
                    return False
                logger.info(
                    f'Fixed patrol: no event seen and current AP {current_ap} is above '
                    f'{self._FIXED_PATROL_L2_AP}, going to L2'
                )
            logger.hr('Fixed patrol L2: move fleets and rescan the whole map')
            self._move_fleets_and_rescan()
            return bool(self._solved_map_event & ALREADY_SOLVED_MAP_EVENTS)
        finally:
            self._in_forced_recovery = False
            # Restore the primary fleet, so that later steps do not work on a wrong fleet.
            self.fleet_set(self.config.OpsiFleet_Fleet)

    def run_auto_search(self, question=True, rescan=None, after_auto_search=True, interrupt=None):
        """
        Clear current zone by running auto search.
        OpSi story mode must be cleared to unlock auto search.

        Args:
            question (bool):
                If clear nearing questions after auto search.
            rescan (bool, str): Whether to rescan the whole map after running auto search.
                This will clear siren scanning devices, siren logging tower,
                visit akashi's shop that auto search missed, and unlock mechanism that requires 2 fleets.
                Accept str also, `current` to scan current camera only,
                `full` to scan current then rescan the whole map

                This option should be disabled in special tasks like OpsiObscure, OpsiAbyssal, OpsiStronghold.
            after_auto_search (bool):
                Whether to call handle_after_auto_search() after auto search
            interrupt (callable | list[callable]): Interrupt callback for auto search.

        Returns:
            int: Number of finished combat
        """
        if rescan is None:
            rescan = self.config.OpsiGeneral_DoRandomMapEvent
        if rescan is True:
            rescan = 'full'
        self.handle_ash_beacon_attack()

        logger.info(f'Run auto search, question={question}, rescan={rescan}')
        finished_combat = 0
        with self.stat.new(
                genre=inflection.underscore(self.config.task.command),
                method=self.config.DropRecord_OpsiRecord
        ) as drop:
            while 1:
                combat = self.os_auto_search_run(drop, interrupt=interrupt)
                finished_combat += combat

                # Record current zone, skip this if no rewards from auto search.
                drop.add(self.device.image)

                self.hp_reset()
                self.hp_get()
                if after_auto_search:
                    if self.is_in_task_explore and not self.zone.is_port:
                        prev = self.zone
                        if self.handle_after_auto_search():
                            self.globe_goto(prev, types='DANGEROUS')
                            continue
                break

            # Rescan
            self._solved_map_event = set()
            self._solved_fleet_mechanism = False
            if question:
                self.clear_question(drop=drop)
            if rescan:
                self.map_rescan(rescan_mode=rescan, drop=drop)

            if drop.count == 1:
                drop.clear()

        return finished_combat

    _solved_map_event = set()
    _solved_fleet_mechanism = 0
    # State of the rescan swipe probe, see _probe_events_after_swipe() and map_rescan_once().
    _rescan_probe_on = False
    _rescan_probe_busy = False
    _rescan_probe_drop = None
    _rescan_probe_solved = False
    # The OpSi map is surrounded by fixed UI: fleet bar on the left, resource bar on top,
    # radar and button column on the right, button row at the bottom. A grid whose click
    # area touches them is not clicked by the probe, because mid-pan the camera is not
    # centered; the planned camera position re-centers it and clicks there instead.
    PROBE_CLICK_SAFE_MARGIN = (170, 140, 250, 110)  # left, top, right, bottom, in 1280x720
    # Same order as the branches of map_rescan_current(), so the probe and the walk agree on
    # which grid would be handled. Fleet mechanisms are left out on purpose: they need the
    # second fleet, which is set up by map_rescan() before it calls the walk.
    PROBE_EVENT_FLAGS = ('is_exploration_container', 'is_exploration_reward', 'is_akashi',
                         'is_scanning_device', 'is_logging_tower')

    def run_strategic_search(self):
        """Run strategic search, then scan the map for events.

        Returns:
            bool: True if the search ran to the end, False if an unexpected error
                interrupted it. Task switching and recovery errors still propagate.
                The event scan runs either way, so a flaky search does not skip the
                rescan of this round. Callers that do more work on top of the scan
                gate it on this return value, e.g. meowfficer farming skips the
                radar patrol when the search was interrupted.
        """
        self.handle_ash_beacon_attack()

        logger.hr('Run strategy search', level=2)
        interrupted = False
        try:
            self.os_auto_search_run(strategic=True)
            self.hp_reset()
            self.hp_get()
        except UNSWALLOWABLE_ERRORS:
            raise
        except Exception as e:
            logger.warning(f'Strategic search interrupted: {e}', exc_info=True)
            interrupted = True

        self._solved_map_event = set()
        self._solved_fleet_mechanism = False
        # A patrol that runs later in this same round reads the primary fleet's radar first
        # anyway, so clearing it here would sweep the same radar twice. Only when no patrol
        # runs does this call carry the question-mark hunt. See _primary_radar_swept_later().
        if not self._primary_radar_swept_later():
            self.clear_question()
        self.map_rescan()
        return not interrupted

    def _siren_device_search_plan(self):
        """
        How many auto search rounds the device dialog is worth, and which fleet runs them.

        Aligned with AzurPilot: probing enemies keeps spawning targets, so it is worth
        three rounds in a row (optionally with a dedicated fleet); probing resources pays
        off in one round; a logging tower / product pillar was already clicked and needs
        none. The mode is read right after the dialog, before any round runs.

        Returns:
            tuple[int, int]: (rounds, fleet). Fleet is 0 when the current fleet is used.
        """
        mode = getattr(self, 'siren_device_mode', None)
        if mode == 'enemy':
            rounds = 3
        elif mode == 'collected':
            rounds = 0
        else:
            rounds = 1
        if rounds <= 1:
            # Only the enemy mode can ask for a specific fleet to farm with.
            return rounds, 0

        task = self.config.task.command
        if task not in ('OpsiHazard1Leveling', 'OpsiMeowfficerFarming'):
            task = 'OpsiHazard1Leveling'
        fleet = self.config.cross_get(keys=f'{task}.OpsiSirenBug.Siren_Fleet', default=0)
        try:
            fleet = int(fleet)
        except (TypeError, ValueError):
            fleet = 0
        return rounds, fleet

    def _probe_grid_clickable(self, grid):
        """
        Args:
            grid: A grid detected in the current view.

        Returns:
            bool: False when its click area touches the fixed UI around the map, or when the
                screen size is unavailable, in which case nothing is clicked from the probe.
        """
        try:
            x1, y1, x2, y2 = grid.button
            height, width = self.device.image.shape[:2]
        except (AttributeError, TypeError, ValueError):
            return False
        left, top, right, bottom = self.PROBE_CLICK_SAFE_MARGIN
        return (x1 >= left and y1 >= top
                and x2 <= width - right and y2 <= height - bottom)

    def _probe_events_after_swipe(self):
        """
        Handle a map event the moment a camera move brings it into sight.

        A full rescan first pans to a map corner to calibrate the camera, then walks from one
        planned camera position to the next, and it only looks for events at those positions.
        The swiping in between never looks, so an event that is already on screen still waits
        for the walk to come back to it: in the 2026-10-05 run a siren logging tower was on
        screen after the first pan swipe but only got clicked 4.2 seconds later, after three
        more swipes.

        This hook is installed only while a rescan owns the camera (map_rescan_once), so no
        other map operation changes behaviour. The view is already detected and predicted by
        the swipe that triggered it, so finding nothing costs no screenshot and no waiting.

        Returns:
            bool: True when something was handled, which tells the camera loop to stop.
        """
        if not self._rescan_probe_on or self._rescan_probe_busy:
            return False

        for flag in self.PROBE_EVENT_FLAGS:
            if flag in self._solved_map_event:
                continue
            grids = self.view.select(**{flag: True})
            if not grids or not getattr(grids[0], flag, False):
                continue
            if not self._probe_grid_clickable(grids[0]):
                # Leave it to the planned camera position, which centers the grid first.
                logger.info(f'Rescan probe: {grids[0]} is too close to the screen edge, '
                            f'leave it to the camera walk')
                return False

            logger.info(f'Rescan probe: a map event came into sight during a camera move, '
                        f'handle it now')
            # Guard against recursion: the handler below can move the camera itself
            # (unreachable Akashi / siren device run the fixed patrol, which pans and would
            # re-enter this probe), and the rescan is not allowed to nest.
            self._rescan_probe_busy = True
            try:
                if self.map_rescan_current(drop=self._rescan_probe_drop):
                    self._rescan_probe_solved = True
                    return True
            except UNSWALLOWABLE_ERRORS:
                raise
            except Exception as e:
                logger.warning(f'Rescan probe failed, continue the camera move: {e}')
            finally:
                self._rescan_probe_busy = False
            return False

        return False

    def map_rescan_current(self, drop=None):
        """

        Args:
            drop:

        Returns:
            bool: If solved a map random event
        """
        grids = self.view.select(is_exploration_container=True)
        if 'is_exploration_container' not in self._solved_map_event \
                and grids and grids[0].is_exploration_container:
            grid = grids[0]
            logger.info(f'Found exploration container on {grid}')
            self.device.click(grid)
            with self.config.temporary(STORY_ALLOW_SKIP=False, STORY_OPTION=1):
                result = self.wait_until_walk_stable(
                    drop=drop, walk_out_of_step=False, confirm_timer=Timer(1.5, count=4))
            if 'event' in result:
                self._solved_map_event.add('is_exploration_container')
                return True
            else:
                return False

        grids = self.view.select(is_exploration_reward=True)
        if 'is_exploration_reward' not in self._solved_map_event and grids and grids[0].is_exploration_reward:
            grid = grids[0]
            logger.info(f'Found exploration reward on {grid}')
            self.device.click(grid)
            result = self.wait_until_walk_stable(drop=drop, walk_out_of_step=False, confirm_timer=Timer(1.5, count=4))
            if 'event' in result:
                self._solved_map_event.add('is_exploration_reward')
                return True
            else:
                return False

        grids = self.view.select(is_akashi=True)
        if 'is_akashi' not in self._solved_map_event and grids and grids[0].is_akashi:
            grid = grids[0]
            logger.info(f'Found Akashi on {grid}')
            # Only take the "already adjacent" shortcut when the current fleet is
            # actually visible AND within one grid of Akashi. During a full rescan
            # the camera is panned away from the fleet, so convert_radar_to_local()
            # would fall back to "camera center is fleet" and wrongly trigger the
            # shortcut, then click Akashi repeatedly while the fleet is far away.
            fleets = self.view.select(is_current_fleet=True)
            if fleets.count == 1 and fleets[0].distance_to(grid) <= 1:
                logger.info(f'Akashi ({grid}) is near current fleet ({fleets[0]})')
                self.handle_akashi_supply_buy(grid)
                self._solved_map_event.add('is_akashi')
                return True
            # Fleet not in sight (or far away): click Akashi and walk to it;
            # wait_until_walk_stable opens the shop once the fleet arrives.
            self.device.click(grid)
            with self.config.temporary(STORY_ALLOW_SKIP=False):
                result = self.wait_until_walk_stable(drop=drop, walk_out_of_step=False)
            if 'akashi' in result:
                self._solved_map_event.add('is_akashi')
                return True
            # Cannot reach Akashi: try the other fleets first, and fall back to the
            # fixed patrol (move the blocking fleet away) when none works.
            grids = self.view.select(is_akashi=True)
            if 'is_akashi' not in self._solved_map_event and grids and grids[0].is_akashi:
                grid = grids[0]
                logger.info('Unable to reach Akashi, try other fleets first')
            else:
                # Akashi "disappeared" from the view: it was clicked but no shop was
                # opened, which means the current fleet cannot reach it. Its icon is
                # hidden by the fleet model and the detection flickers, so a missing
                # re-identification must not be treated as "nothing happened".
                logger.info('Akashi was not identified again, try other fleets first')
            return self._recover_unreachable_akashi(drop, location2node(grid.location))

        grids = self.view.select(is_scanning_device=True)
        if 'is_scanning_device' not in self._solved_map_event and grids and grids[0].is_scanning_device:
            grid = grids[0]
            if location2node(grid.location) in self._unreachable_event_nodes:
                logger.info(f'Siren device on {grid} was already judged unreachable this round, skip')
                return False
            logger.info(f'Found siren probe (scanning device) on {grid}, stop battle plan to handle it')
            # The probe is a special map event, not a normal battle target: click it,
            # wait for the operation popup, confirm it, then rescan. Never skip it to
            # continue the battle plan, otherwise its hidden events are missed.
            self._set_device_state(DEVICE_TARGETED)
            self.device.click(grid)
            self._set_device_state(DEVICE_DIALOG_OPEN)
            with self.config.temporary(STORY_ALLOW_SKIP=False):
                result = self.wait_until_walk_stable(
                    drop=drop, walk_out_of_step=False, confirm_timer=Timer(1.5, count=4))
            reached = 'event' in result
            if not reached:
                # The walk can be blocked by an idle fleet, in which case the device
                # dialog never opens: try the other fleets before giving up.
                reached = self._goto_scanning_device_with_other_fleets(drop=drop)
            if not reached:
                # No fleet could reach the device: remember this grid and fall back to
                # the fixed patrol to move the blocking fleet away. Must return False —
                # returning True would make map_rescan believe the event was handled and
                # spin on the same device.
                logger.info('Unable to reach the siren device, run the fixed patrol')
                self._mark_event_unreachable(location2node(grid.location))
                self._set_device_state(DEVICE_NONE)
                self.execute_fixed_patrol_scan()
                return False

            # How many auto search rounds this dialog is worth, decided from the mode
            # just chosen (read before the rounds, otherwise a pillar collected on the
            # way would overwrite the mode with 'collected' and eat the rounds).
            rounds, siren_fleet = self._siren_device_search_plan()
            mode = getattr(self, 'siren_device_mode', None)
            current_fleet = self.fleet_selector.get()
            switched = False
            if rounds > 1 and siren_fleet > 0 and siren_fleet != current_fleet:
                logger.info(f'Siren device: run the search rounds with fleet {siren_fleet} '
                            f'instead of {current_fleet}')
                self.fleet_set(siren_fleet)
                switched = True
            for index in range(rounds):
                logger.info(f'Siren device ({mode}): auto search {index + 1}/{rounds}')
                self.os_auto_search_run(drop=drop)
            if switched:
                self.fleet_set(current_fleet)

            # Mark it solved before the extra scan, so the second pass cannot re-enter
            # this branch for the same device. The extra scan matters: the device drops
            # its products right next to it, and without it the outer map_rescan stops as
            # soon as it sees one solved event, so those products wait for the next
            # battle plan round (AzurPilot rescans the current view here for the same
            # reason).
            self._solved_map_event.add('is_scanning_device')
            try:
                self.device.screenshot()
                self.update()
                self.map_rescan_current(drop=drop)
            except MapDetectionError:
                logger.warning('Siren device: current view is unreadable, skip the extra rescan')
            self._set_device_state(DEVICE_COMPLETED)
            return True

        grids = self.view.select(is_logging_tower=True)
        if 'is_logging_tower' not in self._solved_map_event and grids and grids[0].is_logging_tower:
            grid = grids[0]
            logger.info(f'Found siren information device (logging tower) on {grid}, stop battle plan to handle it')
            self._set_device_state(DEVICE_TARGETED)
            self.device.click(grid)
            self._set_device_state(DEVICE_DIALOG_OPEN)
            with self.config.temporary(STORY_ALLOW_SKIP=False):
                result = self.wait_until_walk_stable(
                    drop=drop, walk_out_of_step=False, confirm_timer=Timer(1.5, count=4))
            if 'event' in result:
                self._solved_map_event.add('is_logging_tower')
                self._set_device_state(DEVICE_COMPLETED)
                return True
            else:
                self._set_device_state(DEVICE_NONE)
                return False

        grids = self.view.select(is_fleet_mechanism=True)
        if self.is_in_task_explore \
                and 'is_fleet_mechanism' not in self._solved_map_event \
                and grids \
                and grids[0].is_fleet_mechanism:
            grid = grids[0]
            logger.info(f'Found fleet mechanism on {grid}')
            self.device.click(grid)
            self.wait_until_walk_stable(drop=drop, walk_out_of_step=False, confirm_timer=Timer(1.5, count=4))

            if self._solved_fleet_mechanism:
                logger.info('All fleet mechanism are solved')
                self.os_auto_search_run(drop=drop)
                self._solved_map_event.add('is_fleet_mechanism')
                return True
            else:
                logger.info('One of the fleet mechanism is solved')
                self._solved_fleet_mechanism = True
                return True

        logger.info(f'No map event')
        return False

    def map_rescan_once(self, rescan_mode='full', drop=None):
        """
        Args:
            rescan_mode (str): `current` to scan current camera only,
                `full` to scan current then rescan the whole map
            drop:

        Returns:
            bool: If solved a map random event
        """
        result = False
        # A new rescan round gives every event another chance: clear last round's
        # "cannot reach" markers (AzurPilot does the same here).
        self._unreachable_event_nodes = set()

        # Try current camera first
        logger.hr('Map rescan current', level=2)
        self.map_data_init(map_=None)
        self.handle_info_bar()
        self.update()
        if self.map_rescan_current(drop=drop):
            logger.info(f'Map rescan once end, result={True}')
            return True

        if rescan_mode == 'full':
            logger.hr('Map rescan full', level=2)
            # Let every frame the camera produces count as a look for events, not only the
            # planned camera positions. See _probe_events_after_swipe().
            # Save the whole probe state, not just the hook: handling an event found this way
            # can nest a complete rescan (an unreachable Akashi or device runs the fixed
            # patrol, which rescans), and that inner round has to hand the outer one back
            # exactly what it had, instead of switching the probe off for its remaining
            # camera positions and dropping its drop record.
            outer = (self._swipe_probe, self._rescan_probe_on,
                     self._rescan_probe_drop, self._rescan_probe_solved)
            self._rescan_probe_drop = drop
            self._rescan_probe_solved = False
            self._rescan_probe_on = True
            self._swipe_probe = self._probe_events_after_swipe
            try:
                self.map_init(map_=None)
                if self._rescan_probe_solved:
                    logger.info('Map rescan once end, handled an event seen while panning '
                                'to the map edge')
                    return True

                queue = self.map.camera_data
                while len(queue) > 0:
                    logger.hr(f'Map rescan {queue[0]}')
                    queue = queue.sort_by_camera_distance(self.camera)
                    self.focus_to(queue[0], swipe_limit=(6, 5))
                    if self._rescan_probe_solved:
                        logger.info('Map rescan once end, handled an event seen while focusing '
                                    'the camera')
                        result = True
                        break
                    self.focus_to_grid_center(0.3)

                    if self.map_rescan_current(drop=drop):
                        result = True
                        break
                    queue = queue[1:]
            finally:
                (self._swipe_probe, self._rescan_probe_on,
                 self._rescan_probe_drop, self._rescan_probe_solved) = outer

        logger.info(f'Map rescan once end, result={result}')
        return result

    # Sweeps allowed per rescan when the map cannot be read. Most such failures mean "this
    # zone is cleared", which the game never resolves into a readable grid, so a high count
    # only burns a whole sweep (~12s) per try. Three covers a transient black/loading frame.
    _RESCAN_TRIALS = 3

    def map_rescan(self, rescan_mode='full', drop=None):
        if self.zone.is_port:
            logger.info('Current zone is a port, do not need rescan')
            return False
        # Skip only in STANDALONE CL1 (task.command == 'OpsiHazard1Leveling') that also has
        # meowfficer farming off AND fixed patrol off: standalone CL1 preserves AP, so it
        # should not detour to exploration events.
        # With fixed patrol on, this rescan is the cheap first layer of the very same hunt
        # (one sweep, ~12s on a hazard level 1 map), and skipping it makes
        # `hazard_leveling` see an empty `_solved_map_event` and fall through to L0/L1 plus
        # the L2 fleet moving, which costs 50-90s to find what this scan would have found.
        # Under OpsiScheduling the fixed patrol / resume barrier need this rescan to find
        # missed events, so it must never be skipped there.
        if self.is_in_task_cl1_leveling \
                and not self.config.is_task_enabled('OpsiMeowfficerFarming') \
                and not self._forced_move_enabled():
            logger.info('Map rescan skipped: standalone CL1 without meowfficer farming '
                        'and without fixed patrol')
            return False

        last_map_error = None
        for _ in range(self._RESCAN_TRIALS):
            if not self._solved_fleet_mechanism:
                self.fleet_set(self.config.OpsiFleet_Fleet)
            else:
                self.fleet_set(self.get_second_fleet())
            if not self.is_in_task_explore and len(self._solved_map_event):
                logger.info('Solved a map event and not in OpsiExplore, stop rescan')
                logger.attr('Solved_map_event', self._solved_map_event)
                self.fleet_set(self.config.OpsiFleet_Fleet)
                return False
            try:
                result = self.map_rescan_once(rescan_mode=rescan_mode, drop=drop)
            except MapDetectionError as e:
                last_map_error = e
                self.device.screenshot()
                if not self.is_in_map():
                    # A popup, or another task that took over, covers the map: scanning again
                    # would click on that screen, so let the caller handle this frame.
                    raise
                logger.warning(f'Map rescan: no readable map grid ({e}), retrying')
                continue
            if not result:
                logger.attr('Solved_map_event', self._solved_map_event)
                self.fleet_set(self.config.OpsiFleet_Fleet)
                return True

        if last_map_error is not None:
            # A cleared OpSi zone has no free tile for the homography to lock on, and that is
            # its normal steady state, not something worth killing the task over. AzurPilot
            # warns and carries on; the old behaviour spent up to 5 whole sweeps (about a
            # minute) and then raised, so a finished map cost the round *and* a task failure.
            # Events can be missed here, so the log has to say so.
            logger.warning(f'Map rescan gave up after {self._RESCAN_TRIALS} unreadable tries: '
                           f'the map is probably cleared, some events may be missed')
            self.fleet_set(self.config.OpsiFleet_Fleet)
            return False
        logger.attr('Solved_map_event', self._solved_map_event)
        logger.warning('Too many trial on map rescan, stop')
        self.fleet_set(self.config.OpsiFleet_Fleet)
        return False
