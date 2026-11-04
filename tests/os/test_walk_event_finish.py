# -*- coding: utf-8 -*-
"""`wait_until_walk_stable(allow_event_finish=...)` 的提前收尾条件（对齐 AP）。

只有四个条件同时满足才跳过镜头稳定：
侵蚀 1/短猫、调用方允许、事件已彻底办完、地图且计划作战入口可见。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.base.timer import Timer
from module.os.fleet import OSFleet
from module.os_handler.assets import IN_MAP, STRATEGIC_SEARCH_MAP_OPTION_OFF


class WalkStub:
    wait_until_walk_stable = OSFleet.wait_until_walk_stable

    def __init__(self, frames, task='OpsiHazard1Leveling'):
        self.frames = list(frames)
        self.frame = {}
        self.update_os_calls = 0
        self.combat_calls = 0
        self.device = SimpleNamespace(
            screenshot_interval_set=lambda *args, **kwargs: None,
            sleep=lambda *args, **kwargs: None,
        )
        self.config = SimpleNamespace(task=SimpleNamespace(command=task))
        self.view = SimpleNamespace(backend=SimpleNamespace(homo_loca=None))
        self.is_siren_device_confirmed = False
        self.siren_device_mode = None
        self.fleet_show_index = 0

    def loop(self, skip_first=False):
        for index, frame in enumerate(self.frames):
            self.frame = frame
            yield index

    def handle_map_event(self, drop=None):
        return self.frame.get('event', '')

    def handle_retirement(self):
        return False

    def handle_walk_out_of_step(self):
        return False

    def handle_popup_confirm(self, name):
        return False

    def is_in_globe(self):
        return False

    def is_in_storage(self):
        return False

    def is_in_os_mission(self):
        return False

    def handle_os_game_tips(self):
        return False

    def is_in_map_order(self):
        return False

    def combat_appear(self):
        return self.frame.get('combat', False)

    def combat(self, **kwargs):
        self.combat_calls += 1

    def appear(self, button, **kwargs):
        return False

    def interval_clear(self, *args, **kwargs):
        pass

    def handle_akashi_supply_buy(self, *args, **kwargs):
        pass

    def appear_then_click(self, *args, **kwargs):
        return False

    def enemy_searching_appear(self):
        return False

    def enemy_searching_color_initial(self):
        pass

    def is_in_map(self):
        return self.frame.get('in_map', True)

    def update_os(self):
        self.update_os_calls += 1

    def match_template_color(self, button, **kwargs):
        if button is STRATEGIC_SEARCH_MAP_OPTION_OFF:
            return self.frame.get('strategic', False)
        if button is IN_MAP:
            return self.frame.get('in_map', True)
        return False

    def fleet_reset_view(self):
        return False


def walk(stub, allow=True):
    return stub.wait_until_walk_stable(confirm_timer=Timer(60, count=2),
                                       allow_event_finish=allow)


def test_finished_reward_skips_stability_in_fast_farming():
    stub = WalkStub([{'event': 'map_get_items'}, {'strategic': True}])
    assert walk(stub) == 'event'
    assert stub.update_os_calls == 0


def test_default_off_keeps_waiting_for_stability():
    stub = WalkStub([{'event': 'map_get_items'}, {'strategic': True}])
    assert walk(stub, allow=False) == 'event'
    assert stub.update_os_calls == 1


def test_new_story_options_clear_the_completion_flag():
    stub = WalkStub([{'event': 'map_get_items'}, {'event': 'story_skip'}, {'strategic': True}])
    assert walk(stub) == 'event'
    assert stub.update_os_calls >= 1


def test_non_farming_task_keeps_waiting():
    stub = WalkStub([{'event': 'map_get_items'}, {'strategic': True}], task='OpsiDaily')
    assert walk(stub) == 'event'
    assert stub.update_os_calls == 1


def test_missing_auto_search_entry_keeps_waiting():
    stub = WalkStub([{'event': 'map_get_items'}, {'strategic': False}])
    assert walk(stub) == 'event'
    assert stub.update_os_calls == 1


def test_combat_completion_counts_as_finished_event():
    stub = WalkStub([{'combat': True}, {'strategic': True}])
    assert walk(stub) == 'event'
    assert stub.combat_calls == 1
    assert stub.update_os_calls == 0


def test_map_archives_counts_as_finished_event():
    stub = WalkStub([{'event': 'map_archives'}, {'strategic': True}])
    assert walk(stub) == 'event'
    assert stub.update_os_calls == 0


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
