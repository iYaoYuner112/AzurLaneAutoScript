# -*- coding: utf-8 -*-
"""每日委托逐条空域侦察（对齐 AzurPilot 5979fc1cb）。

开荒未完成的普通海域里，每接一条委托、进入海域前都要先侦察一次；
档案海域本来就不需要。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.os.tasks.daily import OpsiDaily
from module.os_handler.mission import MissionHandler


class DailyStub:
    os_finish_daily_mission = OpsiDaily.os_finish_daily_mission
    _is_daily_mission_task = OpsiDaily._is_daily_mission_task
    _os_return_from_unavailable_mission = MissionHandler._os_return_from_unavailable_mission
    _os_deferred_mission_zones = MissionHandler._os_deferred_mission_zones
    _os_defer_mission_zone = MissionHandler._os_defer_mission_zone

    def __init__(self, script, recon_needed):
        self.script = list(script)
        self.recon_needed = recon_needed
        self.recon_scans = []
        self.picks = 0
        self._os_deferred_zones = None
        self._os_mission_index = 0
        self._os_daily_mission_unavailable = False
        self.zone = SimpleNamespace(is_port=False, zone_id=44)
        self.config = SimpleNamespace(
            task=SimpleNamespace(command='OpsiDaily'),
            _opsi_context=SimpleNamespace(current_task='OpsiDaily'),
            OpsiFleet_Fleet=1,
            OpsiFleet_Submarine=False,
            check_task_switch=lambda: None,
        )

    def _os_daily_needs_recon_scan(self):
        return self.recon_needed

    def os_get_next_mission(self, skip_siren_mission=False, skip_unavailable=False, mission_index=0):
        self.picks += 1
        self._os_mission_index = mission_index
        if self.picks > len(self.script):
            return False
        return self.script[self.picks - 1]

    def zone_init(self):
        pass

    def globe_goto(self, zone, types=None, refresh=False):
        pass

    def fleet_set(self, index=1):
        pass

    def os_order_execute(self, recon_scan=True, submarine_call=True):
        self.recon_scans.append(recon_scan)

    def run_auto_search(self, question=True, rescan=None, interrupt=None):
        pass

    def handle_after_auto_search(self):
        pass

    def ensure_no_zone_pinned(self):
        pass

    def os_globe_goto_map(self, skip_first_screenshot=True):
        pass


def test_each_mission_recons_the_zone_when_explore_is_unfinished():
    stub = DailyStub(['pinned_at_mission_zone', False], recon_needed=True)
    assert stub.os_finish_daily_mission() == 1
    assert stub.recon_scans == [True]


def test_no_recon_when_explore_is_complete():
    stub = DailyStub(['pinned_at_mission_zone', False], recon_needed=False)
    assert stub.os_finish_daily_mission() == 1
    assert stub.recon_scans == [False]


def test_archive_mission_is_not_reconnoitered():
    stub = DailyStub(['pinned_at_archive_zone', False], recon_needed=True)
    assert stub.os_finish_daily_mission() == 1
    assert stub.recon_scans == [False]


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
