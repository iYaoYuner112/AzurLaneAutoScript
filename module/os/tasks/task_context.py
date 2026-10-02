"""Opsi task context: run an Operation Siren sub-task under the smart scheduler.

This mirrors AzurPilot's ``opsi_task_context``, mapped onto the Alas scheduler
lifecycle instead of copying it. Three guarantees:

1. **Sub-task identity is preserved** while it runs. ``config.task`` is
   temporarily replaced by the sub-task's own ``Function``, so logs, drop
   records and statistics (``stat.new(genre=...)``) belong to the real sub-task
   rather than to the scheduler that proxied it.

2. **The scheduler stays the owner of the run**. ``config._task_switch_owner``
   keeps pointing at the outer task, so:

   - ``config.task_delay(task=None)`` writes the *owner's* ``NextRun`` -- a
     sub-task can never silently move its own schedule while being proxied;
   - ``config.task_switched()`` compares the *owner* against the scheduler's
     next task, so a proxied run can not mistake itself for a task switch.

3. **Everything is restored in ``finally``**, including when the sub-task raises
   ``TaskEnd`` / ``ActionPointLimit`` / ``OpsiNoContent`` / any exception.

A proxied sub-task has no content to execute this round should not stop the
scheduler: it calls :func:`opsi_no_content`, which raises :class:`OpsiNoContent`
under a proxy (the dispatcher then tries the next candidate task) and keeps the
legacy ``task_delay + task_stop`` behaviour when running standalone.
"""

import typing as t
from contextlib import contextmanager
from dataclasses import dataclass

from module.config.config import Function
from module.logger import logger

__all__ = [
    'OpsiStatus',
    'OpsiTaskResult',
    'TaskDelayRequest',
    'OpsiTaskContext',
    'OpsiNoContent',
    'current_opsi_context',
    'is_running_opsi_proxy',
    'is_opsi_task_switch_disabled',
    'mark_opsi_no_content',
    'pop_opsi_no_content',
    'opsi_no_content',
    'opsi_task_context',
]


class _Missing:
    """Sentinel: the attribute did not exist before the context was entered."""

    def __repr__(self):
        return '<MISSING>'


MISSING = _Missing()


class OpsiStatus:
    """Status of one Opsi scheduling round / sub-task."""

    READY = 'READY'
    RUNNING = 'RUNNING'
    DONE = 'DONE'
    SUCCESS = 'SUCCESS'
    NO_CONTENT = 'NO_CONTENT'
    DELAYED = 'DELAYED'
    INTERRUPTED = 'INTERRUPTED'
    BLOCKED = 'BLOCKED'
    FAILED = 'FAILED'
    NEED_RESCHEDULE = 'NEED_RESCHEDULE'
    NO_TASK = 'NO_TASK'


@dataclass(frozen=True)
class OpsiTaskResult:
    """Result of proxying one Opsi sub-task for one round.

    ``status`` is one of :class:`OpsiStatus`. ``executed`` separates "the task
    really did a round of work" from "the task had nothing to do" -- a
    NO_CONTENT / BLOCKED task must never be treated as a failure.
    """

    status: str
    task: str = ''
    reason: str = ''
    delay_until: t.Optional[t.Any] = None
    map_changed: bool = False
    resource_changed: bool = False
    resume_required: bool = False

    @property
    def executed(self) -> bool:
        return self.status in (
            OpsiStatus.SUCCESS,
            OpsiStatus.DONE,
            OpsiStatus.NEED_RESCHEDULE,
        )

    @property
    def no_content(self) -> bool:
        return self.status == OpsiStatus.NO_CONTENT

    def __str__(self):
        detail = f'{self.task}: {self.status}'
        if self.reason:
            detail += f' ({self.reason})'
        return detail


@dataclass(frozen=True)
class TaskDelayRequest:
    """A delay requested by a sub-task, forwarded to the real task owner."""

    success: t.Optional[bool] = None
    server_update: t.Optional[t.Any] = None
    target: t.Optional[t.Any] = None
    minute: t.Optional[t.Any] = None
    task: t.Optional[str] = None

    def is_empty(self) -> bool:
        return (
            self.success is None
            and self.server_update is None
            and self.target is None
            and self.minute is None
        )

    def apply(self, config):
        if self.is_empty():
            return False
        config.task_delay(
            success=self.success,
            server_update=self.server_update,
            target=self.target,
            minute=self.minute,
            task=self.task,
        )
        return True


@dataclass(frozen=True)
class OpsiTaskContext:
    """Identity of the currently proxied Opsi sub-task."""

    parent_task: str = ''
    current_task: str = ''


def current_opsi_context(config) -> t.Optional[OpsiTaskContext]:
    return getattr(config, '_opsi_context', None)


def is_running_opsi_proxy(config) -> bool:
    """Whether an Opsi sub-task is being proxied right now.

    Replaces the old ``config.task.command == 'OpsiScheduling'`` checks: with
    the task context in place ``config.task`` is the *sub-task* while it runs,
    so identity has to be asked through the context instead.
    """
    context = current_opsi_context(config)
    return context is not None and bool(context.current_task)


def is_opsi_task_switch_disabled(config) -> bool:
    return bool(getattr(config, '_disable_task_switch', False))


def mark_opsi_no_content(config, task_name, reason=''):
    """Remember that ``task_name`` had nothing to do in this round."""
    config._opsi_no_content_task = task_name
    logger.info(f'[大世界-调度] {task_name}: NO_CONTENT ({reason or "no content"})')


def pop_opsi_no_content(config) -> t.Optional[str]:
    task_name = getattr(config, '_opsi_no_content_task', None)
    config._opsi_no_content_task = None
    return task_name


def opsi_no_content(config, task_name, reason='', *, server_update=True, minute=None):
    """Report "no content this round" from inside an Opsi sub-task.

    Under a proxy this raises :class:`OpsiNoContent`, so the dispatcher can try
    the next candidate task instead of stopping the whole scheduler. Standalone
    runs keep the legacy ``task_delay + task_stop`` behaviour.
    """
    if is_running_opsi_proxy(config):
        mark_opsi_no_content(config, task_name, reason)
        raise OpsiNoContent(f'{task_name}: {reason or "no content"}')

    if minute is not None:
        config.task_delay(minute=minute, server_update=server_update)
    else:
        config.task_delay(server_update=server_update)
    config.task_stop()


COIN_TASK_ENABLE_CONFIG = {
    'OpsiStronghold': 'EnableStronghold',
    'OpsiObscure': 'EnableObscure',
    'OpsiAbyssal': 'EnableAbyssal',
    'OpsiMeowfficerFarming': 'EnableMeowfficerFarming',
}


def should_hand_over_to_scheduling(config, task_name, smart_scheduling_enabled) -> bool:
    """Whether a standalone Opsi task must hand control to the scheduler.

    True only when smart scheduling is enabled, the task is one of the coin
    replenish tasks the scheduler manages (so it will really be proxied), and we
    are not already inside a proxy -- otherwise the task would hand over to a
    scheduler that never picks it up.
    """
    if not smart_scheduling_enabled or is_running_opsi_proxy(config):
        return False
    key = COIN_TASK_ENABLE_CONFIG.get(task_name)
    if key is None:
        return False
    return bool(config.cross_get(f'OpsiScheduling.OpsiScheduling.{key}', default=True))


class OpsiNoContent(Exception):
    """Raised by a proxied sub-task that has nothing to do this round."""


@contextmanager
def _temporary_attributes(obj, **attributes):
    """Set attributes for the duration of the block, restoring them exactly.

    Distinguishes "attribute did not exist" from "attribute was None", so the
    context can not leave a fake ``None`` behind on the config object.
    """
    previous = {key: getattr(obj, key, MISSING) for key in attributes}
    for key, value in attributes.items():
        setattr(obj, key, value)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is MISSING:
                try:
                    delattr(obj, key)
                except AttributeError:
                    pass
            else:
                setattr(obj, key, value)


def _make_task_function(config, task_name) -> t.Optional[Function]:
    """Build the sub-task's own ``Function`` from the loaded config data."""
    data = getattr(config, 'data', None)
    if isinstance(data, dict) and task_name in data:
        try:
            return Function(data[task_name])
        except Exception as e:
            logger.warning(f'[大世界-调度] Build function for {task_name} failed: {e}')
    return None


@contextmanager
def opsi_task_context(config, task_name, *, set_task_identity=True, disable_task_switch=True):
    """Run an Opsi sub-task with its own identity but the scheduler as owner.

    Args:
        config: the Alas config object.
        task_name (str): the real sub-task name, e.g. ``OpsiStronghold``.
        set_task_identity (bool): temporarily replace ``config.task`` with the
            sub-task's ``Function`` so logs/statistics belong to the sub-task.
        disable_task_switch (bool): suppress ``check_task_switch()`` inside the
            proxied sub-task. Coin tasks are one-round proxies; letting a switch
            end them mid-round only complicates the delay bookkeeping.

    Yields:
        OpsiTaskContext
    """
    previous_task = getattr(config, 'task', None)
    owner = getattr(config, '_task_switch_owner', None) or previous_task
    owner_command = str(getattr(owner, 'command', '') or '')
    bind_name = owner_command or task_name

    child_task = _make_task_function(config, task_name) if set_task_identity else None
    if child_task is None:
        child_task = previous_task

    context = OpsiTaskContext(parent_task=owner_command, current_task=task_name)
    if set_task_identity:
        logger.info(f'[大世界-调度] 代理执行 {task_name}（调度拥有者 {bind_name}）')

    with _temporary_attributes(
        config,
        task=child_task,
        _opsi_context=context,
        _task_switch_owner=owner,
        _disable_task_switch=disable_task_switch,
    ):
        try:
            config.bind(bind_name, func_list=[task_name])
            yield context
        finally:
            config.bind(bind_name)
