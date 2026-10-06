"""大世界智能调度的推送通知。

推送项和频率对齐 AzurPilot：行动力变化、行动力不足、黄币与行动力双重不足、
本轮代理执行了哪个补币任务。消息一律走「错误推送设置」里的 OnePush 渠道，
每类消息在 `AP_NOTIFY_MIN_INTERVAL_MINUTES` 窗口内最多推一次，避免行动力
缓慢回复或推送渠道挂掉时把手机刷屏。

去重状态和调度决策状态存在同一个 storage 字典里（键名以 Notify 开头），
所以重启实例后仍然记得上次推过的行动力和补币任务。
"""

import re
from datetime import datetime, timedelta

from module.logger import logger

# AzurPilot 的 AP_NOTIFY_MIN_INTERVAL_MINUTES：同类消息的最小推送间隔。
AP_NOTIFY_MIN_INTERVAL_MINUTES = 30

STATE_KEY_LAST_ACTION_POINT = 'NotifyActionPoint'
STATE_KEY_ACTION_POINT = 'NotifyActionPointTime'
STATE_KEY_AP_LOW = 'NotifyActionPointLowTime'
STATE_KEY_COIN_AP_LOW = 'NotifyCoinsAndActionPointLowTime'
STATE_KEY_LAST_COIN_TASK = 'NotifyCoinTask'
STATE_KEY_COIN_TASK_ATTEMPT = 'NotifyCoinTaskAttempt'

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


def _state_get(scheduler, key, default=None):
    return scheduler._get_smart_state().get(key, default)


def _state_update(scheduler, updates: dict):
    state = scheduler._get_smart_state()
    state.update(updates)
    scheduler._save_smart_state(state)


def _parse_time(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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

    尝试时刻也写进 storage：渠道挂着时每轮都重试，等于把一轮一轮的调度变成刷屏。
    """
    now = _now()
    last = _parse_time(_state_get(scheduler, f'{key}_Attempt'))
    if last is None:
        last = _parse_time(_state_get(scheduler, key))
    if last is not None and now - last < timedelta(minutes=AP_NOTIFY_MIN_INTERVAL_MINUTES):
        logger.info(
            f'[大世界-推送] 距上次推送不足 {AP_NOTIFY_MIN_INTERVAL_MINUTES} 分钟，跳过: {key}')
        return False
    _state_update(scheduler, {f'{key}_Attempt': now.isoformat()})
    return True


def notify_action_point_change(scheduler) -> bool:
    """行动力相比上次推送发生变化时推送（读满一轮才推，跌涨都说清）。"""
    if not push_enabled(scheduler):
        return False
    total = getattr(scheduler, '_action_point_total', None)
    if not isinstance(total, int):
        return False

    content = f'总行动力: {total}'
    previous = _state_get(scheduler, STATE_KEY_LAST_ACTION_POINT)
    if isinstance(previous, int):
        delta = total - previous
        if delta == 0:
            logger.info('[大世界-推送] 行动力未发生变化，跳过推送')
            return False
        content += f' 上涨{delta}行动力' if delta > 0 else f' 下跌{-delta}行动力'

    if not _window_passed(scheduler, STATE_KEY_ACTION_POINT):
        return False
    if not send_push(scheduler, '行动力出现变化！', content):
        return False
    _state_update(scheduler, {
        STATE_KEY_ACTION_POINT: _now().isoformat(),
        STATE_KEY_LAST_ACTION_POINT: total,
    })
    return True


def notify_ap_insufficient(scheduler, total_ap, reserve) -> bool:
    """行动力跌破保留线、本轮无事可做。"""
    if not push_enabled(scheduler):
        return False
    if not _window_passed(scheduler, STATE_KEY_AP_LOW):
        return False
    if not send_push(
            scheduler, '智能调度- 行动力不足',
            f'总行动力 {total_ap} 低于最低保留 {reserve}，推迟任务'):
        return False
    _state_update(scheduler, {STATE_KEY_AP_LOW: _now().isoformat()})
    return True


def notify_coins_ap_insufficient(scheduler, yellow_coins, total_ap, coin_target, ap_line) -> bool:
    """黄币没补够，行动力又跌到补黄币的开工线以下。"""
    if not push_enabled(scheduler):
        return False
    if not _window_passed(scheduler, STATE_KEY_COIN_AP_LOW):
        return False
    content = (f'黄币: {yellow_coins}，补黄币阈值: {coin_target}\n'
               f'总行动力 {total_ap} 不足 (需要 {ap_line})\n推迟任务')
    if not send_push(scheduler, '智能调度- 黄币与行动力双重不足', content):
        return False
    _state_update(scheduler, {STATE_KEY_COIN_AP_LOW: _now().isoformat()})
    return True


def notify_coin_task_proxy(scheduler, yellow_coins, total_ap, coin_target, ap_line, task_name) -> bool:
    """本轮代理执行了哪个补币任务：同一个任务连续代理只推一次。"""
    if not push_enabled(scheduler):
        return False

    if _state_get(scheduler, STATE_KEY_LAST_COIN_TASK) == task_name:
        logger.info(f'[大世界-推送] {task_name} 已推送过代理执行，跳过')
        return False

    now = _now()
    window = timedelta(minutes=AP_NOTIFY_MIN_INTERVAL_MINUTES)
    attempt = _state_get(scheduler, STATE_KEY_COIN_TASK_ATTEMPT)
    if isinstance(attempt, dict) and attempt.get('Task') == task_name:
        last = _parse_time(attempt.get('Time'))
        if last is not None and now - last < window:
            logger.info(f'[大世界-推送] {task_name} 推送尝试还在窗口内，跳过')
            return False
    _state_update(scheduler, {
        STATE_KEY_COIN_TASK_ATTEMPT: {'Task': task_name, 'Time': now.isoformat()},
    })

    display = COIN_TASK_DISPLAY_NAMES.get(task_name, task_name)
    content = (f'黄币: {yellow_coins}，补黄币阈值: {coin_target}\n'
               f'总行动力: {total_ap} (需要 {ap_line})\n'
               f'已代理执行一轮{display}获取黄币')
    if not send_push(scheduler, '智能调度- 已代理执行黄币补充任务', content):
        return False
    _state_update(scheduler, {STATE_KEY_LAST_COIN_TASK: task_name})
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
    state = scheduler._get_smart_state()
    removed = False
    for key in (STATE_KEY_LAST_COIN_TASK, STATE_KEY_COIN_TASK_ATTEMPT):
        if key in state:
            state.pop(key)
            removed = True
    if removed:
        scheduler._save_smart_state(state)
