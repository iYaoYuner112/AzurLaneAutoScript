from module.os_handler.action_point import get_action_point_box_priority


def test_action_point_boxes_follow_blue_purple_gold_priority():
    assert get_action_point_box_priority([0, 1, 1, 1], current_ap=0) == [1, 2, 3]


def test_action_point_box_priority_skips_empty_boxes():
    assert get_action_point_box_priority([0, 0, 2, 1], current_ap=0) == [2, 3]
    assert get_action_point_box_priority([0, 0, 0, 1], current_ap=0) == [3]


def test_action_point_box_use_does_not_exceed_200_current_ap():
    boxes = [0, 5, 5, 5]

    assert get_action_point_box_priority(boxes, current_ap=150) == [1, 2]
    assert get_action_point_box_priority(boxes, current_ap=180) == [1]
    assert get_action_point_box_priority(boxes, current_ap=181) == []