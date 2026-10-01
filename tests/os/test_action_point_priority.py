from module.os_handler.action_point import get_action_point_box_priority


def test_action_point_boxes_follow_blue_purple_gold_priority():
    assert get_action_point_box_priority([0, 1, 1, 1]) == [1, 2, 3]


def test_action_point_box_priority_skips_empty_boxes():
    assert get_action_point_box_priority([0, 0, 2, 1]) == [2, 3]
    assert get_action_point_box_priority([0, 0, 0, 1]) == [3]