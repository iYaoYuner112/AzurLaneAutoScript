from datetime import timedelta

from module.base.decorator import cached_property
from module.base.template import Template
from module.base.timer import Timer
from module.base.utils import *
from module.config.utils import DEFAULT_TIME, get_os_next_reset
from module.exception import MapDetectionError
from module.logger import logger
from module.map_detection.utils import fit_points
from module.os.assets import GLOBE_GOTO_MAP
from module.os.globe_detection import GLOBE_MAP_SHAPE
from module.os.globe_operation import GlobeOperation, OSExploreError
from module.os.globe_zone import Zone, ZoneManager
from module.os_handler.assets import *


class MissionAtCurrentZone(Exception):
    pass


class MissionHandler(GlobeOperation, ZoneManager):
    _os_mission_submitted = False
    # How many mission rows the current round has skipped, see `os_get_next_mission()`.
    _os_mission_index = 0
    # Mission zones that turned out to be unenterable in this run, see
    # `_os_defer_mission_zone()`. None means "nothing deferred yet".
    _os_deferred_zones = None

    def _os_return_from_unavailable_mission(self):
        """
        Get back onto the OpSi map after a mission zone turned out to be unenterable.
        """
        self.ensure_no_zone_pinned()
        self.os_globe_goto_map()

    @cached_property
    def _os_mission_checkout_template(self):
        template = Template(MISSION_CHECKOUT.file)
        MISSION_CHECKOUT.ensure_template()
        template.image = MISSION_CHECKOUT.image
        return template

    def _os_mission_checkout_offsets(self):
        """
        Match every mission row on the current page.

        The rows move when the list is scrolled, so a fixed row number cannot be
        used to find the one to click.

        Returns:
            list[tuple]: Checkout offsets, top to bottom.
        """
        offsets = []
        for button in self._os_mission_checkout_template.match_multi(self.device.image, similarity=0.78):
            x, y, _, bottom = button.area
            if (abs(x - MISSION_CHECKOUT.area[0]) <= 20 and 190 <= y and bottom <= 650
                    and color_similar(button.color, MISSION_CHECKOUT.color, threshold=30)):
                offsets.append(area_offset((-20, -20, 20, 20),
                                           (x - MISSION_CHECKOUT.area[0], y - MISSION_CHECKOUT.area[1])))
        return sorted(offsets, key=lambda offset: offset[1])

    def _os_find_checkout_offset_skip_monthly_boss(self, checkout_offset, skip=0):
        """
        Find the checkout row of the next mission, ignoring the monthly boss row.

        Args:
            checkout_offset (tuple): Initial checkout offset.
            skip (int): Missions already skipped in this round, the monthly boss
                not counted.

        Returns:
            tuple | None: Offset of the checkout button to click, None if there
                is no mission left.
        """
        if not skip:
            # Nothing skipped yet: keep the original selection logic, so other
            # callers are not affected.
            row_offset = checkout_offset
            for _ in range(8):
                has_checkout = self.match_template_color(MISSION_CHECKOUT, offset=row_offset, similarity=0.78)
                if has_checkout and not self.appear(MISSION_MONTHLY_BOSS, offset=row_offset):
                    return row_offset
                row_offset = area_offset(row_offset, (0, 110))
            return None

        # A skipped mission stays in the list, so keep looking further down when
        # the missions left do not fit on one page.
        scrolling = None
        stable = Timer(0.3, count=2)
        timeout = Timer(5, count=10)
        scroll_distance = None
        for _ in self.loop():
            if scrolling is not None:
                anchor, anchor_y, previous = scrolling
                image = self.image_crop((600, 170, 1000, 650), copy=False)
                result = cv2.matchTemplate(image, anchor, cv2.TM_CCOEFF_NORMED)
                _, similarity, _, point = cv2.minMaxLoc(result)
                if similarity >= 0.90:
                    distance = anchor_y - (170 + point[1])
                    if scroll_distance is None or abs(distance - scroll_distance) > 2:
                        scroll_distance = distance
                        stable.reset()
                    elif stable.reached():
                        if distance > 2:
                            skip -= sum(MISSION_CHECKOUT.area[1] + offset[1] + 20 - distance < 190
                                        for offset in previous)
                            skip = max(skip, 0)
                            scrolling = None
                            continue
                        if abs(distance) <= 2:
                            # The list did not move after the drag: already at the bottom.
                            return None
                else:
                    stable.reset()
                if timeout.reached():
                    raise MapDetectionError('Unable to confirm the OS mission list scroll position')
                continue

            offsets = self._os_mission_checkout_offsets()
            eligible = [offset for offset in offsets if not self.appear(MISSION_MONTHLY_BOSS, offset=offset)]
            if skip < len(eligible):
                return eligible[skip]
            if not offsets:
                return None
            y = MISSION_CHECKOUT.area[1] + offsets[-1][1] + 20
            anchor_y = max(170, y - 35)
            anchor = self.image_crop((600, anchor_y, 1000, min(650, y + 45)))
            scrolling = (anchor, anchor_y, eligible)
            scroll_distance = None
            stable.reset()
            timeout.reset()
            self.device.drag((820, 550), (820, 330), name='MISSION_SCROLL')

    def _os_deferred_mission_zones(self):
        """
        Mission zones that cannot be entered in this round, see `_os_defer_mission_zone()`.

        AzurPilot stores this in `OpsiDaily.OpsiDaily.DeferredMissions` with an
        expiry at the next daily update. We keep it in memory on purpose: the
        record only has to outlive the current run, and a fresh run retries the
        zone once, which is exactly what a manual re-run would do.

        Returns:
            set[int]: Zone ids whose mission should be skipped.
        """
        if self._os_deferred_zones is None:
            self._os_deferred_zones = set()
        return self._os_deferred_zones

    def _os_defer_mission_zone(self, zone):
        """
        Give up on one mission zone and carry on with the other missions.

        A locked zone stays in the mission list, so a caller that simply retries
        re-picks the very same mission forever.

        Args:
            zone (Zone): Mission zone that cannot be entered.
        """
        self._os_deferred_mission_zones().add(zone.zone_id)
        logger.warning(f'Mission zone {zone} is not enterable, skip its mission and continue the others')

    def get_mission_zone(self):
        """
        Returns:
            Zone:
        """
        area = (341, 72, 1217, 648)
        # Points of the yellow `!`
        image = color_similarity_2d(self.image_crop(area, copy=False), color=(255, 207, 66))
        points = np.array(np.where(image > 235)).T[:, ::-1]
        if not len(points):
            logger.warning('Unable to find mission on OS mission map')

        point = fit_points(points, mod=(1000, 1000), encourage=5) + (0, 11)
        # Location of zone.
        # (2570, 1694) is the shape of os_globe_map.png
        point *= np.array(GLOBE_MAP_SHAPE) / np.subtract(area[2:], area[:2])

        zone = self.camera_to_zone(tuple(point))
        return zone

    def is_in_os_mission(self):
        return self.appear(MISSION_CHECK, offset=(20, 20))

    def os_mission_enter(self, skip_siren_mission=False):
        """
        Enter mission list and claim mission reward.

        Args:
            skip_siren_mission (bool): Skip siren research missions to avoid
                exchanging yellow coins for purple coins.

        Returns:
            tuple: checkout_offset for the mission row, shifted down when a
                siren research mission is skipped.

        Pages:
            in: MISSION_ENTER
            out: MISSION_CHECK
        """
        logger.info('OS mission enter')
        self._os_mission_submitted = False
        checkout_offset = (-20, -20, 20, 20)
        confirm_timer = Timer(2, count=6).start()
        for _ in self.loop():
            # End
            if self.is_in_os_mission() \
                    and not self.appear(MISSION_FINISH, offset=checkout_offset) \
                    and not self.match_template_color(MISSION_CHECKOUT, offset=checkout_offset):
                # No mission found, wait to confirm. Missions might not be loaded so fast.
                if confirm_timer.reached():
                    logger.info('No OS mission found.')
                    break
            elif self.is_in_os_mission() \
                    and self.match_template_color(MISSION_CHECKOUT, offset=checkout_offset):
                # Found one mission.
                logger.info('Found at least one OS missions.')
                break
            else:
                confirm_timer.reset()

            # Click
            if self.appear_then_click(MISSION_ENTER, offset=(200, 5), interval=5):
                confirm_timer.reset()
                continue
            if skip_siren_mission and self.appear(MISSION_SIREN_RESEARCH, offset=checkout_offset):
                # The current mission row is a siren research. Skip it by shifting
                # the checkout offset one row down (about 110 px between rows).
                if self.appear(MISSION_FINISH, offset=checkout_offset):
                    logger.info('Skip Siren Research mission')
                    checkout_offset = area_offset(checkout_offset, (0, 110))
                    confirm_timer.reset()
                    continue
            else:
                if self.appear_then_click(MISSION_FINISH, offset=checkout_offset, interval=2):
                    self._os_mission_submitted = True
                    confirm_timer.reset()
                    continue
            if self.handle_popup_confirm('MISSION_FINISH'):
                confirm_timer.reset()
                continue
            if self.handle_map_get_items():
                confirm_timer.reset()
                continue
            if self.handle_info_bar():
                confirm_timer.reset()
                continue
            if self.appear_then_click(GLOBE_GOTO_MAP, offset=(20, 20), interval=2):
                # Accidentally entered globe
                confirm_timer.reset()
                continue
        return checkout_offset

    def os_mission_quit(self):
        logger.info('OS mission quit')
        for _ in self.loop():
            # End
            # sometimes you have os mission popup without black-blurred background
            # MISSION_QUIT and is_in_map appears
            if not self.appear(MISSION_QUIT, offset=(20, 20)):
                if self.is_in_map():
                    break
            # Click
            if self.appear_then_click(MISSION_QUIT, offset=(20, 20), interval=3):
                continue

    def os_get_next_mission(self, skip_siren_mission=False, skip_unavailable=False, mission_index=0):
        """
        Another method to get os mission. The old one is outdated.
        After clicking MISSION_CHECKOUT, AL switch to target zone directly instead of showing a meaningless map.
        If already at target zone, show info bar and close mission list.

        Args:
            skip_siren_mission (bool): Skip siren research missions.
            skip_unavailable (bool): Skip a mission whose zone cannot be entered
                and carry on with the others, instead of failing the whole round.
                Used by OpsiDaily.
            mission_index (int): Missions already skipped in this round.

        Returns:
            str: pinned_at_mission_zone, already_at_mission_zone, pinned_at_archive_zone,
                mission_zone_unavailable if this mission was only deferred,
                or False if no more mission.
        """
        checkout_offset = self.os_mission_enter(skip_siren_mission=skip_siren_mission)
        self._os_mission_index = 0 if self._os_mission_submitted else mission_index
        if skip_unavailable:
            checkout_offset = self._os_find_checkout_offset_skip_monthly_boss(
                checkout_offset, skip=self._os_mission_index)
        else:
            checkout_offset = self._os_find_checkout_offset_skip_monthly_boss(checkout_offset)
        if checkout_offset is None:
            # Not having enough items to claim a mission still shows a MISSION_CHECKOUT,
            # but the button is transparent, so `match_template_color` rejects it.
            logger.info('No more OS missions')
            self.os_mission_quit()
            return False

        if self.is_in_opsi_explore():
            logger.info('OpsiExplore is under scheduling, accept missions and receive rewards only')
            self.os_mission_quit()
            return False

        logger.info('Checkout os mission')
        for _ in self.loop():
            # End
            if self.is_zone_pinned():
                if self.get_zone_pinned_name() == 'ARCHIVE':
                    logger.info('Pinned at archive zone')
                    self.globe_enter(zone=self.name_to_zone(72))
                    return 'pinned_at_archive_zone'
                else:
                    logger.info('Pinned at mission zone')
                    if skip_unavailable:
                        # Find the real target from the pinned zone instead of taking the
                        # historical placeholder zone 72 as the identity of this mission.
                        self.globe_update()
                        zone = self.get_globe_pinned_zone()
                        if zone.zone_id in self._os_deferred_mission_zones():
                            logger.info(f'Mission zone {zone} is still deferred, continue the others')
                            self._os_return_from_unavailable_mission()
                            return 'mission_zone_unavailable'
                        try:
                            self.globe_enter(zone=zone)
                        except OSExploreError:
                            self._os_defer_mission_zone(zone)
                            self._os_return_from_unavailable_mission()
                            return 'mission_zone_unavailable'
                        return 'pinned_at_mission_zone'
                    self.globe_enter(zone=self.name_to_zone(72))
                    return 'pinned_at_mission_zone'
            if self.is_in_map() and self.info_bar_count():
                logger.info('Already at mission zone')
                return 'already_at_mission_zone'

            if self.appear_then_click(MISSION_CHECKOUT, offset=checkout_offset, interval=2, similarity=0.78):
                continue
            if self.handle_popup_confirm('OS_MISSION_CHECKOUT'):
                # Popup: Submarine will retreat after exiting current zone.
                continue

    def os_mission_overview_accept(self, skip_siren_mission=False):
        """
        Accept all missions in mission overview.

        Args:
            skip_siren_mission (bool): Whether siren research missions are skipped.

        Returns:
            bool: True if all missions accepted or no mission found.
                  False if unable to accept more missions.

        Pages:
            in: is_in_map
            out: is_in_map
        """
        logger.hr('OS mission overview accept', level=1)
        # is_in_map
        self.os_map_goto_globe(unpin=False)
        # is_in_globe
        self.ui_click(MISSION_OVERVIEW_ENTER, check_button=MISSION_OVERVIEW_CHECK,
                      offset=(200, 20), retry_wait=3, additional=self.handle_manjuu,
                      skip_first_screenshot=True)

        timeout = 5
        accept_button_timer = Timer(timeout)
        self.interval_timer[MISSION_OVERVIEW_ACCEPT_SINGLE.name] = accept_button_timer
        self.interval_timer[MISSION_OVERVIEW_ACCEPT.name] = accept_button_timer
        # MISSION_OVERVIEW_CHECK
        success = True
        for _ in self.loop():
            # End
            if self.appear(MISSION_OVERVIEW_EMPTY, offset=(20, 20)):
                success = True
                break
            if self.info_bar_count():
                # 塞壬研究任务同名可重复存在，接取后我们永远不做它，槽位会被它占满。
                # 开着跳过时这是预期状态，按 AzurPilot 的口径算"接取成功"，
                # 否则 os_daily 的 `if success: break` 不成立，会一圈一圈重接个没完。
                if skip_siren_mission:
                    logger.info('Unable to accept missions: multiple siren research missions with the same name')
                    success = True
                else:
                    logger.info('Unable to accept missions, because reached the maximum number of missions')
                    success = False
                break

            if self.handle_manjuu():
                continue
            # Click
            if self.appear_then_click(MISSION_OVERVIEW_ACCEPT, offset=(20, 20), interval=timeout):
                continue
            if self.appear_then_click(MISSION_OVERVIEW_ACCEPT_SINGLE, offset=(20, 20), interval=timeout):
                continue

        # is_in_globe
        self.ui_back(appear_button=MISSION_OVERVIEW_CHECK, check_button=self.is_in_globe,
                     skip_first_screenshot=True)
        # is_in_map
        self.os_globe_goto_map()
        return success

    def is_in_opsi_explore(self):
        """
        Returns:
            bool: If task OpsiExplore is under scheduling.
        """
        enable = self.config.is_task_enabled('OpsiExplore')
        next_run = self.config.cross_get(keys='OpsiExplore.Scheduler.NextRun', default=DEFAULT_TIME)
        next_reset = get_os_next_reset()
        logger.attr('OpsiNextReset', next_reset)
        logger.attr('OpsiExplore', (enable, next_run))
        # -12 hours to handle DST
        # `next_run` might be calculated before DST but it's DST now
        # 2023-03-14 11:15:28.423 | INFO | [OpsiNextReset] 2023-04-01 03:00:00
        # 2023-03-14 11:15:28.425 | INFO | [OpsiExplore] (True, datetime.datetime(2023, 4, 1, 2, 0))
        # 2023-03-14 11:15:28.426 | INFO | OpsiExplore is still running, accept missions only...
        if enable and next_run < next_reset - timedelta(hours=12):
            logger.info('OpsiExplore is still running, accept missions only. '
                        'Missions will be finished when OpsiExplore visits every zones, '
                        'no need to worry they are left behind.')
            return True
        else:
            logger.info('Not in OpsiExplore, able to do OpsiDaily')
            return False
