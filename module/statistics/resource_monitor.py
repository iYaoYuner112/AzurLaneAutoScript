"""资源监视器（Resource Monitor）核心。

纯逻辑，不依赖 device / OCR / logger，只依赖标准库，可无依赖单测。

设计分层：
    Game Screen -> ResourceCollector(OCR) -> ChangeDetector -> ResourceState
        -> ResourceHistory / ResourceChangedEvent -> UI / Log / Alert

本模块提供：
    ResourceState        资源状态快照（OCR 失败保留旧值，不置 0）
    ResourceChangedEvent 资源变化事件
    ChangeDetector       OCR 防抖 + 异常值拒绝
    ResourceHistory      有界历史记录
    ResourceMonitor      门面：start/stop/pause/resume、get/get_all/get_history、
                         on_change、submit

以及保留原有的被动记录器 `record_dashboard_resource`（供现有任务在顺手读到资源时写入
`Alas.Storage.Storage.ResourceMonitor`，向后兼容）。
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Deque, Dict, List, Optional

# 已知资源名（按游戏实际可扩展）。
RESOURCE_OIL = 'oil'
RESOURCE_COIN = 'coin'
RESOURCE_AMMO = 'ammo'
RESOURCE_STEEL = 'steel'
RESOURCE_ALUMINUM = 'aluminum'
RESOURCE_ACTION_POINT = 'action_point'
RESOURCE_YELLOW_COIN = 'yellow_coin'
RESOURCE_PURPLE_COIN = 'purple_coin'
RESOURCE_EVENT_PT = 'event_pt'

# 每种资源的合理上限（超过即视为 OCR 异常，拒绝写入）。数值明显超出游戏范围即可。
DEFAULT_MAX_VALUES = {
    RESOURCE_OIL: 999999,
    RESOURCE_COIN: 999999999,
    RESOURCE_AMMO: 999999,
    RESOURCE_STEEL: 999999999,
    RESOURCE_ALUMINUM: 999999999,
    RESOURCE_ACTION_POINT: 999999,
    RESOURCE_YELLOW_COIN: 999999999,
    RESOURCE_PURPLE_COIN: 999999999,
    RESOURCE_EVENT_PT: 99999999,
}


@dataclass
class ResourceChangedEvent:
    """一次已确认的资源变化。"""
    resource: str
    old_value: Optional[int]
    new_value: int
    delta: int
    timestamp: datetime
    source: str
    confidence: float


@dataclass
class ResourceState:
    """资源状态快照。OCR 失败时不会覆盖旧值。"""
    values: Dict[str, Optional[int]] = field(default_factory=dict)
    timestamp: Optional[datetime] = None
    valid: bool = False
    confidence: float = 0.0

    def get(self, name: str) -> Optional[int]:
        return self.values.get(name)

    def __getitem__(self, name: str) -> Optional[int]:
        return self.values[name]

    def all(self) -> Dict[str, Optional[int]]:
        return dict(self.values)


class ChangeDetector:
    """OCR 防抖 + 异常值拒绝。

    连续 `confirm_reads` 次读到同一值才返回确认值，否则返回 None；
    负数、非整数、超过 `max_values` 上限的值一律拒绝。
    """

    def __init__(self, confirm_reads: int = 2, max_values: Optional[Dict[str, int]] = None):
        self.confirm_reads = confirm_reads
        self.max_values = dict(max_values if max_values is not None else DEFAULT_MAX_VALUES)
        self._pending: Dict[str, tuple] = {}

    def submit(self, name: str, value) -> Optional[int]:
        """提交一次读数，返回确认值，或 None（未确认 / 异常 / 失败）。"""
        if value is None:
            self._pending.pop(name, None)
            return None
        if not isinstance(value, int) or value < 0:
            self._pending.pop(name, None)
            return None
        max_value = self.max_values.get(name)
        if max_value is not None and value > max_value:
            self._pending.pop(name, None)
            return None

        prev, count = self._pending.get(name, (None, 0))
        if value == prev:
            count += 1
        else:
            count = 1
        self._pending[name] = (value, count)
        if count >= self.confirm_reads:
            return value
        return None

    def reset(self, name: Optional[str] = None):
        if name is None:
            self._pending.clear()
        else:
            self._pending.pop(name, None)


class ResourceHistory:
    """有界资源变化历史（不无限堆积到内存）。"""

    def __init__(self, maxlen: int = 500):
        self._events: Deque[ResourceChangedEvent] = deque(maxlen=maxlen)

    def add(self, event: ResourceChangedEvent):
        self._events.append(event)

    def get(self, resource: Optional[str] = None) -> List[ResourceChangedEvent]:
        events = list(self._events)
        if resource is not None:
            events = [e for e in events if e.resource == resource]
        return events

    def clear(self):
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)


class ResourceMonitor:
    """资源监视器门面。

    低优先级、非阻塞：本身不持线程、不截图，`submit()` 由上层（collector）在主线安全点调用。
    start/stop/pause/resume 只切换状态标志；stop/pause 后 submit 直接忽略。
    """

    def __init__(self, confirm_reads: int = 2, history_maxlen: int = 500,
                 max_values: Optional[Dict[str, int]] = None):
        self.state = ResourceState()
        self.detector = ChangeDetector(confirm_reads=confirm_reads, max_values=max_values)
        self.history = ResourceHistory(maxlen=history_maxlen)
        self._listeners: List[Callable[[ResourceChangedEvent], None]] = []
        self._running = False
        self._paused = False

    # ---- 生命周期 ----

    def start(self):
        self._running = True
        self._paused = False

    def stop(self):
        self._running = False
        self._paused = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    # ---- 事件订阅 ----

    def on_change(self, callback: Callable[[ResourceChangedEvent], None]):
        """注册资源变化回调，返回 callback 便于当装饰器用。"""
        self._listeners.append(callback)
        return callback

    # ---- 数据入口 ----

    def submit(self, name: str, value, source: str = 'ocr',
               confidence: float = 1.0, now: Optional[datetime] = None) -> Optional[ResourceChangedEvent]:
        """提交一次资源读数；确认了变化就返回事件，否则返回 None。

        OCR 失败（value=None）或异常值不会覆盖旧状态，也不会产生事件。
        """
        if not self._running or self._paused:
            return None
        confirmed = self.detector.submit(name, value)
        if confirmed is None:
            return None
        old = self.state.get(name)
        if old == confirmed:
            return None  # 无变化
        self.state.values[name] = confirmed
        self.state.timestamp = now or datetime.now()
        self.state.valid = True
        self.state.confidence = confidence
        delta = confirmed - old if old is not None else 0
        event = ResourceChangedEvent(
            resource=name, old_value=old, new_value=confirmed, delta=delta,
            timestamp=self.state.timestamp, source=source, confidence=confidence,
        )
        self.history.add(event)
        for callback in self._listeners:
            try:
                callback(event)
            except Exception:
                # 回调异常不能影响主流程
                pass
        return event

    # ---- 数据读取 ----

    def get(self, name: str) -> Optional[int]:
        return self.state.get(name)

    def get_all(self) -> ResourceState:
        return self.state

    def get_history(self, resource: Optional[str] = None) -> List[ResourceChangedEvent]:
        return self.history.get(resource=resource)


# ---------------------------------------------------------------------------
# 原有的被动记录器（向后兼容）：现有任务在顺手读到资源时写入
# Alas.Storage.Storage.ResourceMonitor，供 Web UI 展示。
# ---------------------------------------------------------------------------

RESOURCE_STORAGE_PATH = 'Alas.Storage.Storage.ResourceMonitor'
RESOURCE_UPDATE_INTERVAL_SECONDS = 20


def record_dashboard_resource(config, name, value, total=None, limit=None, now=None):
    try:
        value = int(value)
        total = int(total) if total is not None else None
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        return False

    now = now or datetime.now()
    resources = config.cross_get(RESOURCE_STORAGE_PATH, default={})
    if not isinstance(resources, dict):
        resources = {}

    previous = resources.get(name, {})
    try:
        previous_time = datetime.strptime(previous.get('Record', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        previous_time = None
    if previous_time and (now - previous_time).total_seconds() < RESOURCE_UPDATE_INTERVAL_SECONDS:
        return False

    record = {
        'Value': value,
        'Record': now.strftime('%Y-%m-%d %H:%M:%S'),
    }
    if total is not None:
        record['Total'] = total
    if limit is not None:
        record['Limit'] = limit
    # Delta since the last recorded value of this resource, for the dashboard.
    prev_value = previous.get('Value')
    if isinstance(prev_value, int):
        record['Delta'] = value - prev_value
    resources[name] = record
    config.modified[RESOURCE_STORAGE_PATH] = resources
    config.save()
    return True
