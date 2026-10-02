from types import SimpleNamespace

from module.os_handler.action_point import (
    ACTION_POINT_BOX,
    ActionPointHandler,
    get_action_point_box_priority,
)


def test_action_point_boxes_follow_blue_purple_gold_priority():
    assert get_action_point_box_priority([0, 1, 1, 1], current_ap=0) == [1, 2, 3]


def test_action_point_box_priority_skips_empty_boxes():
    assert get_action_point_box_priority([0, 0, 2, 1], current_ap=0) == [2, 3]
    assert get_action_point_box_priority([0, 0, 0, 1], current_ap=0) == [3]


def test_overflowing_box_is_kept_as_last_resort():
    """开箱会超过 200 的箱子不再被丢掉，而是排到最后兜底（对齐 AP）。"""
    boxes = [0, 5, 5, 5]

    # 150+20 / +50 不溢出排在前面，150+100=250 溢出 → 最后
    assert get_action_point_box_priority(boxes, current_ap=150) == [1, 2, 3]
    # 只剩金箱且开箱会溢出：旧实现会过滤成 []，现在返回 [3] 作为兜底
    assert get_action_point_box_priority([0, 0, 0, 1], current_ap=150) == [3]
    # 一个箱子都没有才是空
    assert get_action_point_box_priority([0, 0, 0, 0], current_ap=150) == []


# ---- 行动力箱 vs 石油购买：不要让开箱抢在买油前面 ----

class FakeConfig:
    def __init__(self, buy_limit):
        self.OpsiGeneral_BuyActionPointLimit = buy_limit
        self.OS_ACTION_POINT_PRESERVE = 200


def make_handler(buy_limit, current=100, total=1600):
    stub = SimpleNamespace(
        config=FakeConfig(buy_limit),
        is_smart_scheduling_enabled=True,
        _action_point_current=current,
        _action_point_total=total,
        _action_point_box=[0, 5, 5, 5],
        box_calls=[],
        quit_calls=0,
    )
    stub._is_in_action_point = lambda: True
    stub.action_point_safe_get = lambda: None
    stub.action_point_set_button = lambda index: stub.box_calls.append(index)

    def use():
        stub._action_point_current += ACTION_POINT_BOX[stub.box_calls[-1]]

    stub.action_point_use = use
    stub.action_point_quit = lambda: setattr(stub, 'quit_calls', stub.quit_calls + 1)
    return stub


def call_handle_action_point(stub, cost=70):
    return ActionPointHandler.handle_action_point(
        stub, None, None, cost=cost, keep_current_ap=False)


def test_no_box_top_up_when_ap_is_already_enough():
    """行动力够开工就不开箱（对齐 AP：箱子只在行动力不足时才用）。"""
    stub = make_handler(buy_limit=0, current=100)
    assert call_handle_action_point(stub, cost=70) is True
    assert stub.box_calls == []
    assert stub.quit_calls == 1


def test_no_box_top_up_even_with_oil_purchase_enabled():
    """开了买油更不能拿箱子顶上，否则买油永远轮不到（用户报的 bug）。"""
    stub = make_handler(buy_limit=3, current=100)
    assert call_handle_action_point(stub, cost=70) is True
    assert stub.box_calls == []
    assert stub.quit_calls == 1


def test_boxes_are_used_only_when_ap_is_insufficient():
    """行动力不足且没开买油时，才用箱子把行动力补到开工线。"""
    stub = make_handler(buy_limit=0, current=50, total=1600)
    assert call_handle_action_point(stub, cost=70) is True
    assert stub.box_calls == [1]
    assert stub._action_point_current == 70


def test_overflowing_box_is_used_instead_of_giving_up():
    """只剩会溢出的箱子时也照开（对齐 AP），不再直接 ActionPointLimit。"""
    stub = make_handler(buy_limit=0, current=190, total=1600)
    stub._action_point_box = [0, 0, 0, 5]
    assert call_handle_action_point(stub, cost=200) is True
    assert stub.box_calls == [3]