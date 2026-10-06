import onepush.core
import yaml
from onepush import get_notifier
from onepush.core import Provider
from onepush.exceptions import OnePushException
from onepush.providers.custom import Custom
from requests import Response

from module.logger import logger

onepush.core.log = logger

# onepush 的内部请求不传 timeout，推送服务器无响应时会永久阻塞调用线程
# （智能调度的推送是在决策线程里同步发的，一挂就把整轮调度挂住），
# 所以给所有推送请求注入默认超时：(连接超时, 读取超时)，单位秒。
PUSH_REQUEST_TIMEOUT = (10, 30)

# 只在未 patch 过时包装：模块被重复加载时 Provider.request 已是包装函数，
# 二次包装会因模块 dict 原地更新导致原函数引用丢失、无限递归
if not getattr(Provider.request, '_timeout_patched', False):
    _original_provider_request = Provider.request

    def _provider_request_with_timeout(method, url, **kwargs):
        kwargs.setdefault('timeout', PUSH_REQUEST_TIMEOUT)
        return _original_provider_request(method, url, **kwargs)

    _provider_request_with_timeout._timeout_patched = True
    Provider.request = staticmethod(_provider_request_with_timeout)


def handle_notify(_config: str, **kwargs) -> bool:
    try:
        config = {}
        for item in yaml.safe_load_all(_config):
            config.update(item)
    except Exception:
        logger.error("Fail to load onepush config, skip sending")
        return False
    try:
        provider_name: str = config.pop("provider", None)
        if provider_name is None:
            logger.info("No provider specified, skip sending")
            return False
        notifier: Provider = get_notifier(provider_name)
        required: list[str] = notifier.params["required"]
        config.update(kwargs)

        # pre check
        for key in required:
            if key not in config:
                logger.warning(
                    f"Notifier {notifier.name} require param '{key}' but not provided"
                )

        if isinstance(notifier, Custom):
            if "method" not in config or config["method"] == "post":
                config["datatype"] = "json"
            if not ("data" in config or isinstance(config["data"], dict)):
                config["data"] = {}
            if "title" in kwargs:
                config["data"]["title"] = kwargs["title"]
            if "content" in kwargs:
                config["data"]["content"] = kwargs["content"]

        if provider_name.lower() == "gocqhttp":
            access_token = config.get("access_token")
            if access_token:
                config["token"] = access_token

        resp = notifier.notify(**config)
        if resp is None:
            # onepush 内部请求异常被吞（连接失败/超时/SSL 重试失败）时返回 None，
            # 必须显式报失败，否则卡死或推送不可达时日志里无任何失败痕迹
            logger.warning("Push notify failed!")
            logger.warning("No response from the push server (connection failed or timed out)")
            return False
        if isinstance(resp, Response):
            if resp.status_code != 200:
                logger.warning("Push notify failed!")
                logger.warning(f"HTTP Code:{resp.status_code}")
                return False
            else:
                if provider_name.lower() == "gocqhttp":
                    return_data: dict = resp.json()
                    if return_data["status"] == "failed":
                        logger.warning("Push notify failed!")
                        logger.warning(
                            f"Return message:{return_data['wording']}")
                        return False
    except OnePushException:
        logger.error("Push notify failed")
        return False
    except Exception as e:
        # don't show any exceptions because exceptions contain variable traceback
        logger.error(e)
        return False

    logger.info("Push notify success")
    return True
