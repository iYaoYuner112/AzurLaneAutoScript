"""大世界智能调度的推送通知。

推送项和频率对齐 AzurPilot：行动力变化、行动力不足、黄币与行动力双重不足、
本轮代理执行了哪个补币任务。消息一律走「错误推送设置」里的 OnePush 渠道，
每类消息在 `AP_NOTIFY_MIN_INTERVAL_MINUTES` 窗口内最多推一次，避免行动力
缓慢回复或推送渠道挂掉时把手机刷屏。

去重状态分两层，和 AzurPilot 一致：
- **窗口时间戳挂在 config 对象上**（纯内存）。`Alas` 每跑完一个任务都会重建 Config，
  所以任务边界、进程重启都会把窗口清零：新一轮调度第一次读到行动力变化就立刻推一条。
- **上次推送时的总行动力落在 `OpsiScheduling.Storage.Storage`**（键名 `NotifyActionPoint`，
  AzurPilot 用它的 cl1_database 干这件事）。这样重启后行动力没变就不会重复推送。
"""

import re
from datetime import datetime, timedelta

from module.logger import logger

# AzurPilot 的 AP_NOTIFY_MIN_INTERVAL_MINUTES：同类消息的最小推送间隔。
AP_NOTIFY_MIN_INTERVAL_MINUTES = 30

# 落盘：上次推送行动力变化时的总行动力
STATE_KEY_LAST_ACTION_POINT = 'NotifyActionPoint'

# 内存（挂在 config 对象上）：各类消息的上次成功推送时刻，
# 以及上次尝试推送时刻（`<键名>_attempt`，渠道挂掉时也在窗口内不再重试）
RUNTIME_ATTR_ACTION_POINT = 'opsi_notify_action_point_time'
RUNTIME_ATTR_ACTION_POINT_LOW = 'opsi_notify_action_point_low_time'
RUNTIME_ATTR_COINS_AP_LOW = 'opsi_notify_coins_ap_low_time'
# 内存：本轮代理执行过并已推送的补币任务名，以及该任务的上次推送尝试
RUNTIME_ATTR_COIN_TASK = 'opsi_notify_coin_task'
RUNTIME_ATTR_COIN_TASK_ATTEMPT = 'opsi_notify_coin_task_attempt'

COIN_TASK_DISPLAY_NAMES = {
    'OpsiStronghold': '塞壬要塞',
    'OpsiObscure': '隐秘海域',
    'OpsiAbyssal': '深渊坐标',
    'OpsiMeowfficerFarming': '耄耋相接',
}

PROVIDER_NULL = re.compile(r'provider\s*[:=]\s*null')

# 没配渠道时每轮决策都会走到这里，提示过一次就别再刷日志
_no_channel_warned = False


def _now() -> datetime:
    return datetime.now()


def _window() -> timedelta:
    return timedelta(minutes=AP_NOTIFY_MIN_INTERVAL_MINUTES)


def _last_pushed_action_point(scheduler):
    """上次推送行动力变化时的总行动力；没推过返回 None。"""
    value = scheduler._get_smart_state().get(STATE_KEY_LAST_ACTION_POINT)
    return value if isinstance(value, int) else None


def _mark_action_point_pushed(scheduler, total_ap):
    state = scheduler._get_smart_state()
    state[STATE_KEY_LAST_ACTION_POINT] = total_ap
    scheduler._save_smart_state(state)


def _flag_get(scheduler, key):
    return getattr(scheduler.config, key, None)


def _flag_set(scheduler, key, value):
    setattr(scheduler.config, key, value)


def _flag_clear(scheduler, key):
    try:
        delattr(scheduler.config, key)
    except AttributeError:
        pass


def _push_config_ready(config) -> bool:
    """推送渠道是否配置了：默认值 `provider: null` 表示没配，什么都不发。"""
    raw = getattr(config, 'Error_OnePushConfig', None)
    if isinstance(raw, dict):
        provider = raw.get('provider')
        return provider is not None and str(provider).lower() != 'null'
    if not isinstance(raw, str) or not raw.strip():
        return False
    return PROVIDER_NULL.search(raw.lower()) is None


def push_enabled(scheduler) -> bool:
    """大世界推送的总开关：调度未启用、开关关掉、或没配渠道都不推。"""
    global _no_channel_warned
    config = scheduler.config
    if not scheduler.is_smart_scheduling_enabled:
        return False
    if not getattr(config, 'OpsiGeneral_NotifyOpsiMail', False):
        return False
    if not _push_config_ready(config):
        if not _no_channel_warned:
            _no_channel_warned = True
            logger.info('[大世界-推送] 「错误推送设置」里没有配置渠道，跳过大世界推送')
        return False
    return True


def send_push(scheduler, title, content) -> bool:
    """同步发送一条推送；失败只留日志，不影响正在跑的任务。"""
    config = scheduler.config
    name = getattr(config, 'config_name', 'Alas')
    try:
        from module.notify import handle_notify
        sent = bool(handle_notify(
            config.Error_OnePushConfig,
            title=f'Alas <{name}> {title}',
            content=content,
        ))
    except Exception as e:
        logger.error(f'[大世界-推送] 推送异常: {e}')
        return False
    if sent:
        logger.info(f'[大世界-推送] 已推送: {title}')
    else:
        logger.warning(f'[大世界-推送] 推送失败: {title}')
    return sent


def _window_passed(scheduler, key) -> bool:
    """距上次推送（或上次尝试推送）是否已超过最小间隔。

    尝试时刻也一起记：渠道挂着时每轮都重试，等于把一轮一轮的调度变成刷屏。
    两个时刻都只存在 config 对象上，任务边界重建 Config 就自然清零。
    """
    now = _now()
    attempt = _flag_get(scheduler, f'{key}_attempt')
    last = attempt if isinstance(attempt, datetime) else _flag_get(scheduler, key)
    if isinstance(last, datetime) and now - last < _window():
        logger.info(
            f'[大世界-推送] 距上次推送不足 {AP_NOTIFY_MIN_INTERVAL_MINUTES} 分钟，跳过: {key}')
        return False
    _flag_set(scheduler, f'{key}_attempt', now)
    return True


def _mark_sent(scheduler, key):
    _flag_set(scheduler, key, _now())


def notify_action_point_change(scheduler) -> bool:
    """行动力相比上次推送发生变化时推送（涨了多少、跌了多少都说清）。"""
    if not push_enabled(scheduler):
        return False
    total = getattr(scheduler, '_action_point_total', None)
    if not isinstance(total, int):
        return False

    content = f'总行动力: {total}'
    previous = _last_pushed_action_point(scheduler)
    if previous is not None:
        delta = total - previous
        if delta == 0:
            logger.info('[大世界-推送] 行动力未发生变化，跳过推送')
            return False
        content += f' 上涨{delta}行动力' if delta > 0 else f' 下跌{-delta}行动力'

    if not _window_passed(scheduler, RUNTIME_ATTR_ACTION_POINT):
        return False
    if not send_push(scheduler, '行动力出现变化！', content):
        return False
    _mark_sent(scheduler, RUNTIME_ATTR_ACTION_POINT)
    _mark_action_point_pushed(scheduler, total)
    return True


def notify_ap_insufficient(scheduler, total_ap, reserve) -> bool:
    """行动力跌破保留线、本轮无事可做。"""
    if not push_enabled(scheduler):
        return False
    if not _window_passed(scheduler, RUNTIME_ATTR_ACTION_POINT_LOW):
        return False
    if not send_push(
            scheduler, '智能调度- 行动力不足',
            f'总行动力 {total_ap} 低于最低保留 {reserve}，推迟任务'):
        return False
    _mark_sent(scheduler, RUNTIME_ATTR_ACTION_POINT_LOW)
    return True


def notify_coins_ap_insufficient(scheduler, yellow_coins, total_ap, coin_target, ap_line) -> bool:
    """黄币没补够，行动力又跌到补黄币的开工线以下。"""
    if not push_enabled(scheduler):
        return False
    if not _window_passed(scheduler, RUNTIME_ATTR_COINS_AP_LOW):
        return False
    content = (f'黄币: {yellow_coins}，补黄币阈值: {coin_target}\n'
               f'总行动力 {total_ap} 不足 (需要 {ap_line})\n推迟任务')
    if not send_push(scheduler, '智能调度- 黄币与行动力双重不足', content):
        return False
    _mark_sent(scheduler, RUNTIME_ATTR_COINS_AP_LOW)
    return True


def notify_coin_task_proxy(scheduler, yellow_coins, total_ap, coin_target, ap_line, task_name) -> bool:
    """本轮代理执行了哪个补币任务：同一个任务连续代理只推一次。"""
    if not push_enabled(scheduler):
        return False

    if _flag_get(scheduler, RUNTIME_ATTR_COIN_TASK) == task_name:
        logger.info(f'[大世界-推送] {task_name} 已推送过代理执行，跳过')
        return False

    now = _now()
    attempt = _flag_get(scheduler, RUNTIME_ATTR_COIN_TASK_ATTEMPT)
    if (
        isinstance(attempt, tuple) and len(attempt) == 2
        and attempt[0] == task_name
        and isinstance(attempt[1], datetime)
        and now - attempt[1] < _window()
    ):
        logger.info(f'[大世界-推送] {task_name} 推送尝试还在窗口内，跳过')
        return False
    _flag_set(scheduler, RUNTIME_ATTR_COIN_TASK_ATTEMPT, (task_name, now))

    display = COIN_TASK_DISPLAY_NAMES.get(task_name, task_name)
    content = (f'黄币: {yellow_coins}，补黄币阈值: {coin_target}\n'
               f'总行动力: {total_ap} (需要 {ap_line})\n'
               f'已代理执行一轮{display}获取黄币')
    if not send_push(scheduler, '智能调度- 已代理执行黄币补充任务', content):
        return False
    _flag_set(scheduler, RUNTIME_ATTR_COIN_TASK, task_name)
    return True


def notify_coin_task_disabled(scheduler) -> bool:
    """补币任务一个都没启用，调度只能推迟到服务器刷新，因此不设推送窗口。"""
    if not push_enabled(scheduler):
        return False
    return send_push(
        scheduler, '智能调度- 未启用黄币补充任务',
        '请至少启用耄耋相接、隐秘海域、深渊坐标或塞壬要塞中的一项')


def clear_coin_task_push_state(scheduler) -> None:
    """黄币补够、恢复侵蚀 1 后清掉补币推送状态。

    否则下次再进入补币阶段时，同一个任务会被去重吞掉、永远只推第一次。
    """
    _flag_clear(scheduler, RUNTIME_ATTR_COIN_TASK)
    _flag_clear(scheduler, RUNTIME_ATTR_COIN_TASK_ATTEMPT)
