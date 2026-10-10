import numpy as np

from module.config.config import TaskEnd
from module.config.utils import get_os_reset_remain
from module.exception import ScriptError
from module.logger import logger
from module.map.map_grids import SelectedGrids
from module.os.globe_operation import OSExploreError
from module.os.map import OSMap
from module.os.tasks.task_context import current_opsi_context
from module.os_handler.action_point import ActionPointLimit
from module.os_handler.assets import MISSION_COMPLETE_POPUP
from module.ui.assets import OS_CHECK
from module.ui.page import page_os


class OpsiDaily(OSMap):
    # Rounds a port mission may make no progress in before the whole round is given up.
    OS_DAILY_STUCK_PORT_RETRY = 3

    # Set by `os_finish_daily_mission()` when a round ends without finishing anything and
    # retrying would repeat the very same missions (an unenterable zone, a stuck port).
    _os_daily_mission_unavailable = False

    def os_port_mission(self):
        """
        Visit all ports and do the daily mission in it.
        """
        logger.hr('OS port mission', level=1)
        ports = ['NY City', 'Dakar', 'Taranto', 'Gibraltar', 'Brest', 'Liverpool', 'Kiel', 'St. Petersburg']
        if np.random.uniform() > 0.5:
            ports.reverse()

        for port in ports:
            port = self.name_to_zone(port)
            logger.hr(f'OS port daily in {port}', level=2)
            self.globe_goto(port)

            self.run_auto_search()
            self.handle_after_auto_search()

    def _os_daily_mission_complete_check(self):
        """Check if the mission complete popup appeared (out of OS map)."""
        return not self.appear(OS_CHECK, offset=(20, 20)) and \
            self.appear(MISSION_COMPLETE_POPUP, offset=(20, 20))

    _os_mission_complete = False

    def daily_interrupt_check(self):
        """
        Check if the daily mission auto search should be interrupted.

        Returns:
            bool: True when the mission is complete and no meowfficer is searching.
        """
        if not self._os_mission_complete and self._os_daily_mission_complete_check():
            self._os_mission_complete = True

        if self._os_mission_complete and self.no_meowfficer_searching():
            return True
        return False

    def os_daily_set_keep_mission_zone(self):
        """Save the current zone into OpsiDaily_MissionZones config."""
        zones = prev = self.config.OpsiDaily_MissionZones
        zones = [] if zones is None else str(zones).split()
        if str(self.zone.zone_id) not in zones:
            zones.append(str(self.zone.zone_id))
        new = ' '.join(zones)
        if prev != new:
            self.config.OpsiDaily_MissionZones = new
            logger.info(f'[OS DAILY] Save uncleared sector {self.zone.zone_id}')

    def os_daily_clear_all_mission_zones(self):
        """
        Clear all zones recorded in OpsiDaily_MissionZones.

        Only runs on the last day before OS reset (get_os_reset_remain() == 0).
        """
        if get_os_reset_remain() > 0:
            logger.info('[OS DAILY] Not the last day of month, skip clearing mission zones')
            return

        logger.info('[OS DAILY] Last day of month, cleanup all uncleared sectors')

        def os_daily_check_zone(zone):
            return zone.hazard_level in [3, 4, 5, 6] and zone.region != 5 and not zone.is_port

        try:
            zones = self.config.OpsiDaily_MissionZones
            zones = [] if zones is None else str(zones).split()
            clear_zones = SelectedGrids([self.name_to_zone(zone) for zone in zones]) \
                .delete(SelectedGrids([self.zone])) \
                .filter(os_daily_check_zone) \
                .sort_by_clock_degree(center=(1252, 1012), start=self.zone.location)
        except ScriptError:
            logger.warning('Mission zones config invalid, skip clearing mission zones')
            zones = []

        for zone in clear_zones:
            logger.hr(f'[OS DAILY] Cleaning uncleared sector {zone.zone_id}', level=1)
            try:
                self.globe_goto(zone, types='SAFE', refresh=True)
            except ActionPointLimit:
                continue
            except OSExploreError:
                # A locked zone must not poison the rest of the list: it is still in the
                # config, so the next run picks it up again. Mark it deferred as well, so
                # the daily mission flow of this run does not keep retrying its mission
                # (AzurPilot defers it to the next daily reset in `_os_defer_mission_zone()`).
                logger.warning(f'[OS DAILY] Uncleared sector {zone.zone_id} is not enterable, skip it')
                self._os_defer_mission_zone(zone)
                self._os_return_from_unavailable_mission()
                continue
            self.fleet_set(self.config.OpsiFleet_Fleet)
            self.os_order_execute(recon_scan=False, submarine_call=False)
            self.run_auto_search(question=False, rescan=False)
            self._os_daily_retrieve_events()
            self.handle_after_auto_search()
            if str(zone.zone_id) in zones:
                zones.remove(str(zone.zone_id))
                self.config.OpsiDaily_MissionZones = ' '.join(zones)

        if not len(zones):
            self.config.OpsiDaily_MissionZones = None
        logger.info('[OS DAILY] Monthly cleanup finished')

    def _os_daily_retrieve_events(self):
        """Retrieve events after clearing a mission zone.

        Order: clear primary fleet question -> map rescan -> switch fleet 2/3/4 to
        clear questions. Stops as soon as any event is solved.
        """
        primary = self.config.OpsiFleet_Fleet
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False
        event_solved = False

        # Step 1: clear primary fleet question.
        self.fleet_set(primary)
        self.device.screenshot()
        if self.clear_question():
            event_solved = True

        # Step 2: rescan map if not solved.
        if not event_solved:
            self.map_rescan()
            if self._solved_map_event:
                event_solved = True

        # Step 3: switch fleet 2/3/4 to clear questions if not solved.
        if not event_solved:
            for fleet in [1, 2, 3, 4]:
                if fleet == primary:
                    continue
                self.fleet_set(fleet)
                self.device.screenshot()
                if self.clear_question():
                    event_solved = True
                    break
            self.fleet_set(primary)

    def _is_daily_mission_task(self):
        """
        Whether the running task is OpsiDaily itself, including while OpsiScheduling proxies it.
        `config.task.command` alone is not enough there, the proxy keeps its own identity in the
        Opsi task context (see module/os/tasks/task_context).
        """
        if self.config.task.command == 'OpsiDaily':
            return True
        context = current_opsi_context(self.config)
        return getattr(context, 'current_task', '') == 'OpsiDaily'

    def os_finish_daily_mission(self, skip_siren_mission=False, keep_mission_zone=False, question=True, rescan=None):
        """
        Finish all daily mission in Operation Siren.
        Suggest to run os_port_daily to accept missions first.

        A mission whose zone cannot be entered is deferred and the flow carries on
        with the other missions, instead of giving the whole round up: a locked zone
        stays in the mission list, so retrying it is the only real failure mode.
        A port mission that auto search cannot finish stops the round after
        `OS_DAILY_STUCK_PORT_RETRY` no-progress rounds and returns 0.

        Args:
            skip_siren_mission (bool): Skip siren research missions.
            keep_mission_zone (bool): Keep the mission zone, interrupt auto search
                when the mission is complete instead of fully clearing the zone.
            question (bool): refer to run_auto_search
            rescan (None, bool): refer to run_auto_search

        Returns:
            int: Number of missions finished
        """
        logger.hr('OS finish daily mission', level=1)
        # Only the daily mission flow drops an unenterable mission zone and carries on with the
        # rest; archive and month-end runs must still see the failure (AzurPilot gates it on the
        # running task the same way).
        skip_unavailable = self._is_daily_mission_task()
        count = 0
        mission_index = 0
        self._os_daily_mission_unavailable = False
        # A port mission that auto search cannot finish (a dialogue, a pickup, a shop
        # interaction) keeps handing the very same zone back, so this loop would refresh it
        # forever. Count the no-progress rounds on one port and stop the round.
        stuck_port_zone_id = None
        stuck_port_retry = 0
        abort_due_to_stuck_port = False
        while True:
            if skip_unavailable:
                # The deferred mission stays in the list, so hand the index over to let
                # `_os_find_checkout_offset_skip_monthly_boss()` scroll past it.
                result = self.os_get_next_mission(skip_siren_mission=skip_siren_mission,
                                                  skip_unavailable=True,
                                                  mission_index=mission_index)
                mission_index = self._os_mission_index
            else:
                result = self.os_get_next_mission(skip_siren_mission=skip_siren_mission)
            if not result:
                break
            if result == 'mission_zone_unavailable':
                self._os_daily_mission_unavailable = True
                mission_index += 1
                continue

            if result != 'pinned_at_archive_zone':
                # The name of archive zone is "archive zone", which is not an existing zone.
                # After archive zone, it go back to previous zone automatically.
                self.zone_init()
            if result == 'already_at_mission_zone':
                zone = self.zone
                if skip_unavailable and zone.zone_id in self._os_deferred_mission_zones():
                    self._os_daily_mission_unavailable = True
                    mission_index += 1
                    continue
                try:
                    self.globe_goto(zone, refresh=True)
                except OSExploreError:
                    if not skip_unavailable:
                        raise
                    self._os_defer_mission_zone(zone)
                    self._os_return_from_unavailable_mission()
                    self._os_daily_mission_unavailable = True
                    mission_index += 1
                    continue
            self.fleet_set(self.config.OpsiFleet_Fleet)
            self.os_order_execute(
                recon_scan=False,
                submarine_call=self.config.OpsiFleet_Submarine and result != 'pinned_at_archive_zone')
            if keep_mission_zone and not self.zone.is_port:
                interrupt = [self.daily_interrupt_check, self.is_meowfficer_searching]
                self._os_mission_complete = False
            else:
                interrupt = None
            try:
                finished_combat = self.run_auto_search(question, rescan, interrupt=interrupt)
                self.handle_after_auto_search()
            except TaskEnd:
                self.ui_ensure(page_os)
                if keep_mission_zone:
                    self.os_daily_set_keep_mission_zone()
                finished_combat = 0

            # At a port `finished_combat` is 0 for every mission, so the zone id alone does
            # not mean anything: it has to be the same port, over and over, with no battle.
            if self.zone.is_port and finished_combat == 0 and result in (
                    'already_at_mission_zone', 'pinned_at_mission_zone'):
                zone_id = self.zone.zone_id
                if stuck_port_zone_id == zone_id:
                    stuck_port_retry += 1
                else:
                    stuck_port_zone_id = zone_id
                    stuck_port_retry = 1

                if stuck_port_retry >= self.OS_DAILY_STUCK_PORT_RETRY:
                    logger.warning(f'[OS DAILY] Mission seems stuck in port zone {zone_id} '
                                   f'({self.zone}): auto search made no progress in '
                                   f'{stuck_port_retry} rounds, stop this round to avoid '
                                   f'refreshing the zone forever')
                    abort_due_to_stuck_port = True
                    # AzurPilot only returns 0 here, but the outer OpsiDaily loop re-accepts
                    # missions and calls this method again, which walks the very same
                    # no-progress rounds a second time. Mark the round unproductive so that
                    # loop gives up instead.
                    self._os_daily_mission_unavailable = True
                    break
            else:
                stuck_port_zone_id = None
                stuck_port_retry = 0

            count += 1
            if not keep_mission_zone:
                self.config.check_task_switch()

        if abort_due_to_stuck_port:
            # Return 0 so the outer OpsiDaily flow exits this round cleanly.
            return 0

        return count

    def os_daily(self):
        # Finish existing missions first
        # No need anymore, os_mission_overview_accept() is able to handle
        # self.os_finish_daily_mission()

        # Clear tuning samples daily
        if self.config.OpsiDaily_UseTuningSample:
            # Both jobs live in the storage, so keep it open when the logger is used as well
            # and enter/leave it only once (AzurPilot does the same).
            self.tuning_sample_use(quit=not self.config.OpsiGeneral_UseLogger)
        if self.config.OpsiGeneral_UseLogger:
            self.logger_use()

        # Siren research skip and keep mission zone are only supported on CN server.
        if self.config.OpsiDaily_SkipSirenResearchMission and self.config.SERVER not in ['cn']:
            logger.warning('Skip Siren Research mission is only supported on CN server')
            self.config.OpsiDaily_SkipSirenResearchMission = False
        if self.config.OpsiDaily_KeepMissionZone and self.config.SERVER not in ['cn']:
            logger.warning('Keep mission zone is only supported on CN server')
            self.config.OpsiDaily_KeepMissionZone = False

        skip_siren_mission = self.config.OpsiDaily_SkipSirenResearchMission
        self._os_daily_mission_unavailable = False
        while True:
            # If unable to receive more dailies, finish them and try again.
            success = self.os_mission_overview_accept(skip_siren_mission=skip_siren_mission)
            # Re-init zone name
            # MISSION_ENTER appear from the right,
            # need to confirm that the animation has ended,
            # or it will click on MAP_GOTO_GLOBE
            self.zone_init()
            finished = self.os_finish_daily_mission(
                skip_siren_mission=skip_siren_mission,
                keep_mission_zone=self.config.OpsiDaily_KeepMissionZone)
            if finished and skip_siren_mission:
                continue
            if self.is_in_opsi_explore():
                self.os_port_mission()
                break
            # Nothing was finished: either every mission left sits in a zone that cannot be
            # entered, or a port mission made no progress at all. Re-accepting would only
            # walk over the very same missions again.
            if success or (not finished and self._os_daily_mission_unavailable):
                break

        if self.config.OpsiDaily_KeepMissionZone:
            if self.zone.is_azur_port:
                logger.info('[OS DAILY] Already at azur port')
            else:
                self.globe_goto(self.zone_nearest_azur_port(self.zone))
            self.os_daily_clear_all_mission_zones()
        self.config.task_delay(server_update=True)
