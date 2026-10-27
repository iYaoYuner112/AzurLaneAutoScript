"""防止行动力溢出的延迟请求交接（回归：每分钟空转 / 运行中关不掉）。

背景：防溢出代跑智能调度时，调度会 `task_delay(server_update=True)`。如果它直接
写配置，防溢出主循环的收尾会把这个延迟覆盖成「1 分钟后重来」，于是每分钟空转
一次、且期间无法关闭任务。这里约定：处于防溢出上下文时，调度只把请求登记到
`overflow.request` 上，由防溢出任务统一采用。
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from module.os.tasks.scheduling import request_delay_for_owner
from module.os.tasks.task_context import (
    OverflowDelay,
    OpsiTaskContext,
    TaskDelayRequest,
    prevent_overflow_context,
)


class _FakeConfig:
    def __init__(self):
        self.calls = 0


def _make_request():
    return TaskDelayRequest(server_update='00:00')


def test_no_overflow_context_returns_false_without_building_request():
    """不在防溢出里：返回 False，且不构造请求（非防溢出路径行为不变）。"""
    config = _FakeConfig()
    built = []

    def make():
        built.append(1)
        return _make_request()

    assert request_delay_for_owner(config, make) is False
    assert built == [], '非防溢出路径不应构造延迟请求'


def test_overflow_context_records_request_once():
    """在防溢出里：登记一次，后续调用不覆盖已有请求。"""
    config = _FakeConfig()
    with prevent_overflow_context(config):
        assert request_delay_for_owner(config, _make_request) is True
        overflow = config._opsi_context.overflow
        assert isinstance(overflow.request, TaskDelayRequest)
        assert overflow.request.server_update == '00:00'

        first = overflow.request
        assert request_delay_for_owner(
            config, lambda: TaskDelayRequest(minute=120)) is True
        assert overflow.request is first, '已有请求不应被后来的请求覆盖'


def test_overflow_container_survives_proxy_context():
    """代理层（子任务）里仍然能看到同一个 overflow 容器。"""
    config = _FakeConfig()
    with prevent_overflow_context(config) as overflow:
        assert isinstance(overflow, OverflowDelay)
        assert request_delay_for_owner(config, _make_request) is True
        assert config._opsi_context.overflow is overflow
        assert isinstance(config._opsi_context, OpsiTaskContext)


if __name__ == '__main__':
    test_no_overflow_context_returns_false_without_building_request()
    test_overflow_context_records_request_once()
    test_overflow_container_survives_proxy_context()
    print('all passed')
