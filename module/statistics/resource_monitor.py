from datetime import datetime


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
    resources[name] = record
    config.cross_set(RESOURCE_STORAGE_PATH, resources)
    return True