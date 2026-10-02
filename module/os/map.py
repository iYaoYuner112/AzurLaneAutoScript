import time
from contextlib import suppress

import inflection

from module.base.timer import Timer
from module.config.config import OS_MAP_STALE_KEY, OS_RESUME_RECOVERY_KEY
from module.config.utils import get_os_reset_remain
from module.exception import CampaignEnd, GameStuckError, GameTooManyClickError, MapDetectionError, \
    MapWalkError, RequestHumanTakeover, ScriptEnd, ScriptError
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
    AntiLoopGuard,
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

        The re-sync does: invalidate stale scan caches -> FULL MAP RESCAN ->
        rebuild targets (done later by the target selector). Fleet positions are
        re-read by the fixed patrol / radar check when they are actually needed.

        Smart scheduling resume: when OpsiScheduling itself was preempted by
        another task (OS_RESUME_RECOVERY_KEY set), the barrier does NOT rescan
        unconditionally. It defers to the one-shot Auto Search probe in
        `os_init()`: if the auto search starts normally the map state is
        considered still valid; only when the probe has no effect does it fall
        back to one full map rescan (see `_os_resume_recovery_auto_search()`).
        This applies to every zone, including 22/44/154 which normally skip the
        first auto search -- those get an extra probe call site in `os_init()`.
        """
        if not self._os_map_was_interrupted():
            logger.info('[OS RESUME] Continue without interruption')
            return
        logger.info('[OS][MAP] Resync map state (stale detected)')
        logger.info(f'[OS RESUME] Current zone: {self.zone}')

        # If a Siren device interaction was in progress when the task was
        # interrupted, do NOT replay any stale click. Re-observe the UI: the full
        # rescan below re-detects the device and re-decides from the real state.
        if self._device_state in DEVICE_INTERRUPTIBLE_STATES:
            logger.info(f'[OS][DEVICE] interruption detected during state={self._device_state}')
            logger.info('[OS][DEVICE] recovery: re-observing current UI (no stale click)')

        # Invalidate stale scan caches before re-observing the map.
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False

        # Smart scheduling resume: OpsiScheduling was preempted by another task.
        # One-shot: consume the recovery flag immediately, no matter which path
        # is taken below, so a later resume can never probe twice.
        if self._os_resume_recovery_available():
            self.config.cross_set(OS_RESUME_RECOVERY_KEY, False)
            logger.info('[OS][RESUME] OpsiScheduling resumed after external task interruption')
            logger.info('[OS][RESUME] Map state marked stale')
            logger.info('[OS][RESUME] Deferring resync to the one-shot Auto Search recovery probe')
            self._os_resume_probe_pending = True
            # Stale flag stays set; it is cleared by the probe result.
            return

        logger.info('[OS RESUME] Running FULL MAP RESCAN after interruption')
        try:
            self.map_rescan(rescan_mode='full')
        except (ScriptEnd, CampaignEnd, GameStuckError, GameTooManyClickError,
                RequestHumanTakeover):
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

    # One-shot probe flag: set by ensure_map_state_current() when OpsiScheduling
    # resumes after external preemption, consumed by the first auto search in
    # os_init() (see `_os_resume_recovery_auto_search()`).
    _os_resume_probe_pending = False

    def _os_resume_recovery_auto_search(self):
        """
        First auto search probe after OpsiScheduling is resumed from an external
        preemption (smart scheduling resume barrier).

        Try ONE auto search with the existing functions:
        - If auto search really runs (it starts and the daemon loop completes
          normally, with combat activity detected by the existing battle-count
          detector), the map state is still valid: resume scheduling directly
          and skip the full map rescan.
        - If it has no effect (auto search can not even start / unexpected
          failure, never by repeated clicking), fall back to ONE FULL MAP RESCAN
          to re-sync fleets and targets, then resume scheduling.

        "Real" activity is judged with the existing in-project signals only:
        the daemon's own unlock check (which raises RequestHumanTakeover when the
        auto search option stays invisible), and `_auto_search_battle_count`,
        incremented by `on_auto_search_battle_count_add()` whenever
        `combat_appear()` sees a battle actually start.

        One-shot: the probe flag is consumed at entry, so this runs at most
        once per preemption-resume cycle and can never loop.
        """
        self._os_resume_probe_pending = False
        logger.hr('OS resume recovery: first auto search probe', level=2)
        logger.info('[OS][RESUME] Trying one Auto Search recovery')
        self.on_auto_search_battle_count_reset()
        started = False
        combat = 0
        try:
            combat = self.run_auto_search(
                question=False, rescan=False, after_auto_search=False)
            started = True
        except RequestHumanTakeover:
            # The daemon's unlock check could not see the auto search option.
            # Auto search can not even start: treat as no effect, do not click
            # again, fall back to a full map rescan.
            logger.warning('[OS][RESUME] Auto search option unavailable, treat as no effect')
        battle_count = self._auto_search_battle_count
        if started:
            logger.info(
                f'[OS][RESUME] Auto Search started successfully '
                f'(combat={combat}, battle_count={battle_count})')
            self.config.cross_set(OS_MAP_STALE_KEY, False)
            self._os_resume_recovery_finished()
            return

        logger.warning('[OS][RESUME] Auto Search attempt had no effect')
        logger.info('[OS][RESUME] Falling back to FULL MAP RESCAN')
        logger.info('[OS][RESUME] FULL MAP RESCAN after scheduling interruption')
        # Same cache invalidation as the regular resume barrier: events solved
        # during the probe belong to the pre-resumption map data.
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False
        try:
            self.map_rescan(rescan_mode='full')
        except (ScriptEnd, CampaignEnd, GameStuckError, GameTooManyClickError,
                RequestHumanTakeover):
            raise
        except Exception as e:
            logger.warning(f'[OS RESUME] full rescan failed, continue: {e}')
        self.config.cross_set(OS_MAP_STALE_KEY, False)
        self._os_resume_recovery_finished()

    def _os_resume_recovery_finished(self):
        """Log the resume barrier completion and hand back to the scheduler."""
        logger.info('[OS][RESUME] Map state synchronized')
        logger.info('[OS][RESUME] Rebuilding scheduling targets')

    @property
    def _device_state(self):
        return self.config.cross_get(DEVICE_STATE_KEY, default=DEVICE_NONE) or DEVICE_NONE

    def _set_device_state(self, state):
        self.config.cross_set(DEVICE_STATE_KEY, state)
        logger.info(f'[OS][DEVICE] state={state}')

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

        # Clear current zone
        if self.zone.zone_id in [22, 44, 154]:
            logger.info('In zone 22, 44, 154, skip running first auto search')
            if self._os_resume_probe_pending:
                # These zones do not run a first auto search of their own, so
                # the resume probe gets its own call site here. Without it a
                # resume in these zones could only ever do a full map rescan.
                self._os_resume_recovery_auto_search()
                self.handle_after_auto_search()
            self.handle_ash_beacon_attack()
        else:
            if self._os_resume_probe_pending:
                # Smart scheduling resumed after external preemption: this
                # first auto search doubles as the resume probe. It falls back
                # to a full map rescan by itself when it has no effect.
                self._os_resume_recovery_auto_search()
            else:
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

        Args:
            drop:

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
                self._question_unreachable = question_seen
                return False

            question_seen = True
            logger.info(f'Found question mark on {grid}')
            self.handle_info_bar()

            self.update_os()
            self.view.predict()
            self.view.show()

            try:
                grid = self.convert_radar_to_local(grid)
            except KeyError:
                # The question mark sits outside the current local view (e.g. the
                # fleet is at the map edge and the question is one grid beyond it).
                # It can not be clicked now; mark it unreachable so the fixed patrol
                # L2 moves a fleet to bring it into view.
                logger.warning('Question mark is outside the local view, mark unreachable')
                self._question_unreachable = True
                return False
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
            elif 'event' in result and grid.is_scanning_device:
                self._solved_map_event.add('is_scanning_device')
                self.os_auto_search_run(drop=drop)
                return True
            else:
                logger.warning(f'Arrive question with unexpected result: {result}, expected: {grid.str}')
                self._question_unreachable = True
                continue

        logger.warning('Failed to goto question mark after 5 trail, '
                       'this might be 2 adjacent fleet mechanism, stopped')
        self._question_unreachable = question_seen
        return False

    def clear_question_any_fleet(self):
        primary = self.config.OpsiFleet_Fleet
        solved_events = set()
        question_unreachable = False
        try:
            for fleet in [primary] + [index for index in (1, 2, 3, 4) if index != primary]:
                logger.info(f'[FIXED PATROL][L0] Check Fleet {fleet}')
                if not self._set_fixed_patrol_fleet(fleet):
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
                logger.info(f'[FIXED PATROL][L0] Fleet {fleet}: no actionable question')
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
            try:
                clickable_grid = self.convert_global_to_local(target_grid.location)
            except KeyError:
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
        as an event is solved, so it never blindly cycles all four fleets. The
        AntiLoopGuard is only a safety net, not a controller.

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

                if self._fixed_patrol_loop_guard.check(None, None, fleet, 'move'):
                    logger.error(
                        f'[OS] Stuck while moving fleets: no progress for '
                        f'{self._fixed_patrol_loop_guard.repeat_count} iterations, stop'
                    )
                    return False

                if not self._move_fleet_to_patrol(fleet, columns[fleet]):
                    continue

                # The blocking fleet is away, rescan the whole map to find events.
                self._solved_map_event = set()
                self._solved_fleet_mechanism = False
                logger.info(f'[FIXED PATROL][L2] FULL RESCAN after Fleet {fleet} movement')
                try:
                    self.map_rescan(rescan_mode='full')
                except (ScriptEnd, CampaignEnd, GameStuckError, GameTooManyClickError,
                        RequestHumanTakeover):
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

    def _forced_move_enabled(self):
        """
        Read the fixed patrol switch, tolerating the legacy level numbers.

        Returns:
            bool: True if fixed patrol is enabled.
        """
        value = self.config.OpsiScheduling_ExecuteFixedPatrolScan
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

    _fixed_patrol_loop_guard = AntiLoopGuard(max_repeats=3)
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
        if not self.zone or self.zone.is_port or self.zone.hazard_level != 1:
            # The landing grids are defined for the hazard level 1 map.
            return False
        if self._solved_map_event:
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

        self._fixed_patrol_loop_guard.reset()
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

    def run_strategic_search(self):
        self.handle_ash_beacon_attack()

        logger.hr('Run strategy search', level=2)
        self.os_auto_search_run(strategic=True)

        self.hp_reset()
        self.hp_get()
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False
        self.clear_question()
        self.map_rescan()

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
            else:
                return False

        grids = self.view.select(is_scanning_device=True)
        if 'is_scanning_device' not in self._solved_map_event and grids and grids[0].is_scanning_device:
            grid = grids[0]
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
            self.os_auto_search_run(drop=drop)
            if 'event' in result:
                self._solved_map_event.add('is_scanning_device')
                self._set_device_state(DEVICE_COMPLETED)
                return True
            else:
                self._set_device_state(DEVICE_NONE)
                return False

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
            self.map_init(map_=None)
            queue = self.map.camera_data
            while len(queue) > 0:
                logger.hr(f'Map rescan {queue[0]}')
                queue = queue.sort_by_camera_distance(self.camera)
                self.focus_to(queue[0], swipe_limit=(6, 5))
                self.focus_to_grid_center(0.3)

                if self.map_rescan_current(drop=drop):
                    result = True
                    break
                queue = queue[1:]

        logger.info(f'Map rescan once end, result={result}')
        return result

    def map_rescan(self, rescan_mode='full', drop=None):
        if self.zone.is_port:
            logger.info('Current zone is a port, do not need rescan')
            return False
        # Only skip in STANDALONE CL1 (task.command == 'OpsiHazard1Leveling'),
        # not under smart scheduling: standalone CL1 preserves AP, so visiting
        # exploration events is skipped unless meowfficer farming is also enabled.
        # Under OpsiScheduling the fixed patrol / resume barrier need this rescan
        # to find missed events, so it must never be skipped there.
        if self.is_in_task_cl1_leveling and not self.config.is_task_enabled('OpsiMeowfficerFarming'):
            logger.info('Map rescan skipped: standalone CL1 without meowfficer farming')
            return False

        last_map_error = None
        for _ in range(5):
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
                # The game may be on a black/loading screen right after auto search,
                # so the first map detection can fail with "no free tile". Retry a few
                # times instead of letting the error kill the whole task.
                last_map_error = e
                logger.warning('Map rescan: map detection failed (black screen), retrying')
                self.device.screenshot()
                continue
            if not result:
                logger.attr('Solved_map_event', self._solved_map_event)
                self.fleet_set(self.config.OpsiFleet_Fleet)
                return True

        if last_map_error is not None:
            raise last_map_error
        logger.attr('Solved_map_event', self._solved_map_event)
        logger.warning('Too many trial on map rescan, stop')
        self.fleet_set(self.config.OpsiFleet_Fleet)
        return False
