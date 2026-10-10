from module.base.timer import Timer
from module.logger import logger
from module.os_handler.assets import *
from module.os_shop.assets import PORT_SUPPLY_CHECK
from module.os_shop.shop import OSShop
from module.ui.assets import BACK_ARROW

# Azur Lane ports have PORT_GOTO_MISSION, PORT_GOTO_SUPPLY, PORT_GOTO_DOCK.
# Red axis ports have PORT_GOTO_SUPPLY.
# Use PORT_GOTO_SUPPLY as checker.
PORT_CHECK = PORT_GOTO_SUPPLY


class PortHandler(OSShop):
    def port_enter(self):
        """
        Pages:
            in: IN_MAP
            out: PORT_CHECK
        """
        logger.info('Port enter')
        for _ in self.loop():
            if self.appear(PORT_CHECK, offset=(20, 20)):
                break
            if self.appear_then_click(PORT_ENTER, offset=(20, 20), interval=5):
                continue
            if self.handle_map_event():
                continue
        # Buttons at the bottom has an animation to show
        pass  # Already ensured in ui_click

    def port_quit(self, skip_first_screenshot=True):
        """
        Pages:
            in: PORT_CHECK
            out: IN_MAP
        """
        logger.info('Port quit')
        # Quitting the port clicks the back arrow; a too fast click can land outside the map
        # and raise the "leave OpSi?" popup, whose confirm would quit OpSi.
        self.ui_back(appear_button=PORT_CHECK, check_button=self.is_in_map,
                     skip_first_screenshot=skip_first_screenshot,
                     additional=self.handle_leave_os_popup)
        # Buttons at the bottom has an animation to show
        self.wait_os_map_buttons()

    def port_mission_accept(self):
        """
        Accept all missions in port.

        Deprecated since 2022.01.13, missions are shown only in overview, no longer to be shown at ports.

        Returns:
            bool: True if all missions accepted or no mission found.
                  False if unable to accept more missions.

        Pages:
            in: PORT_CHECK
            out: PORT_CHECK
        """
        if not self.appear(PORT_MISSION_RED_DOT):
            logger.info('No available missions in this port')
            return True

        self.ui_click(PORT_GOTO_MISSION, appear_button=PORT_CHECK, check_button=PORT_MISSION_CHECK,
                      skip_first_screenshot=True)

        confirm_timer = Timer(1.5, count=3).start()
        success = True
        for _ in self.loop():
            if self.appear_then_click(PORT_MISSION_ACCEPT, offset=(20, 20), interval=0.2):
                confirm_timer.reset()
                continue
            else:
                # End
                if confirm_timer.reached():
                    success = True
                    break

            if self.info_bar_count():
                logger.info('Unable to accept missions, because reached the maximum number of missions')
                success = False
                break

        self.ui_back(appear_button=PORT_MISSION_CHECK, check_button=PORT_CHECK, skip_first_screenshot=True)
        return success

    def port_shop_enter(self):
        """
        Pages:
            in: PORT_CHECK
            out: PORT_SUPPLY_CHECK
        """
        self.ui_click(PORT_GOTO_SUPPLY, appear_button=PORT_CHECK, check_button=PORT_SUPPLY_CHECK,
                      skip_first_screenshot=True)
        # Port items has an animation to show
        self.device.sleep(0.5)
        self.device.screenshot()

    def port_shop_quit(self, skip_first_screenshot=True):
        """
        Leave the port supply shop and get back to the port page.

        Backing out can land on the OpSi order overview instead of the port, and backing
        out of that can land on the map, so the plain back button alone is not enough.
        Loop with a timeout and recover from both (AzurPilot does the same).

        Args:
            skip_first_screenshot (bool):

        Pages:
            in: PORT_SUPPLY_CHECK
            out: PORT_CHECK
        """
        logger.info('Port shop quit')
        self.interval_clear([PORT_SUPPLY_CHECK, PORT_CHECK, ORDER_CHECK])

        timeout = Timer(10, count=30).start()
        order_quit_used = False
        while 1:
            if timeout.reached():
                logger.warning('Port shop quit timed out, fall back to the back button')
                self.ui_back(appear_button=PORT_SUPPLY_CHECK, check_button=PORT_CHECK,
                             skip_first_screenshot=True)
                break

            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # End
            if self.appear(PORT_CHECK, offset=(20, 20)):
                break

            # Accidentally entered the order overview, close it with its own quit button.
            if self.appear(ORDER_CHECK, offset=(20, 20)):
                logger.warning('Entered the order overview by accident, quitting it')
                self.order_quit()
                order_quit_used = True
                self.interval_clear([PORT_SUPPLY_CHECK, PORT_CHECK, ORDER_CHECK])
                timeout.reset()
                continue

            # Leaving the order overview can drop us on the map, so enter the port again.
            if order_quit_used and self.is_in_map():
                logger.info('Dropped on the map after leaving the order overview, entering the port again')
                self.port_enter()
                order_quit_used = False
                self.interval_reset(PORT_CHECK)
                continue

            # Click
            if self.appear(PORT_SUPPLY_CHECK, offset=(20, 20), interval=3):
                self.device.click(BACK_ARROW)
                self.interval_reset(PORT_SUPPLY_CHECK)
                continue

    def port_dock_repair(self):
        """
        Repair all ships.

        Pages:
            in: PORT_CHECK
            out: PORT_CHECK
        """
        self.ui_click(PORT_GOTO_DOCK, appear_button=PORT_CHECK, check_button=PORT_DOCK_CHECK,
                      skip_first_screenshot=True)

        repaired = False
        for _ in self.loop():
            # End
            if self.info_bar_count():
                break
            if repaired and self.appear(PORT_DOCK_CHECK, offset=(20, 20)):
                break

            # PORT_DOCK_CHECK is button to repair all.
            if self.appear_then_click(PORT_DOCK_CHECK, offset=(20, 20), interval=2):
                continue
            if self.handle_popup_confirm('DOCK_REPAIR'):
                repaired = True
                continue

        self.ui_back(appear_button=PORT_DOCK_CHECK, check_button=PORT_CHECK, skip_first_screenshot=True)
