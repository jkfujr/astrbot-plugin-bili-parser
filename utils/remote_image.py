from functools import partial
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Image


REMOTE_IMAGE_FLAG = "_bili_parser_remote_image_send_enabled"
ORIGINAL_SEND_ATTR = "_bili_parser_original_send"
REMOTE_IMAGE_RESULT_FLAG = "_bili_parser_remote_image_result"


def enable_remote_image_delivery(event: Any, result: Any) -> Any:
    """启用图片直传；旧 aiocqhttp 由插件侧发送包装实现，未来框架字段同步设置。"""
    _enable_framework_remote_flag(result)

    if _is_aiocqhttp_event(event) and _result_has_remote_image(result):
        _mark_remote_image_result(result)
        _install_aiocqhttp_send_wrapper(event)

    return result


def _enable_framework_remote_flag(result: Any) -> None:
    use_remote_image_url = getattr(result, "use_remote_image_url", None)
    if callable(use_remote_image_url):
        use_remote_image_url(True)
        return

    if hasattr(result, "use_remote_image_url_"):
        result.use_remote_image_url_ = True


def _mark_remote_image_result(result: Any) -> None:
    setattr(result, REMOTE_IMAGE_RESULT_FLAG, True)

    original_derive = getattr(result, "derive", None)
    if not callable(original_derive):
        return

    setattr(result, "derive", partial(_derive_with_remote_image_flag, original_derive))


def _derive_with_remote_image_flag(original_derive: Any, chain: Any = None) -> Any:
    derived = original_derive(chain)
    setattr(derived, REMOTE_IMAGE_RESULT_FLAG, True)
    _enable_framework_remote_flag(derived)
    return derived


def _is_aiocqhttp_event(event: Any) -> bool:
    get_platform_name = getattr(event, "get_platform_name", None)
    if callable(get_platform_name):
        return get_platform_name() == "aiocqhttp"

    platform_meta = getattr(event, "platform_meta", None)
    return getattr(platform_meta, "name", "") == "aiocqhttp"


def _result_has_remote_image(result: Any) -> bool:
    return any(
        _get_remote_image_url(component)
        for component in (getattr(result, "chain", None) or [])
    )


def _install_aiocqhttp_send_wrapper(event: Any) -> None:
    if getattr(event, REMOTE_IMAGE_FLAG, False):
        return

    original_send = getattr(event, "send", None)
    if not callable(original_send):
        return

    setattr(event, ORIGINAL_SEND_ATTR, original_send)

    setattr(event, "send", partial(_send_with_remote_image, event, original_send))
    setattr(event, REMOTE_IMAGE_FLAG, True)


async def _send_with_remote_image(
    event: Any,
    original_send: Any,
    message_chain: Any,
) -> None:
    if _can_send_aiocqhttp_direct(message_chain):
        try:
            await _send_aiocqhttp_direct(event, message_chain)
            setattr(event, "_has_send_oper", True)
            return
        except Exception as exc:
            logger.warning(f"[BiliParser] 图片直传失败，回退 AstrBot 默认发送: {exc}")

    await original_send(message_chain)


def _can_send_aiocqhttp_direct(message_chain: Any) -> bool:
    if not getattr(message_chain, REMOTE_IMAGE_RESULT_FLAG, False):
        return False

    chain = getattr(message_chain, "chain", None) or []
    if not chain:
        return False

    has_remote_image = False
    for component in chain:
        component_type = _component_type(component)
        if component_type in {"File", "Node", "Nodes", "Record", "Video"}:
            return False

        if isinstance(component, Image):
            if not _get_remote_image_url(component):
                return False
            has_remote_image = True

    return has_remote_image


async def _send_aiocqhttp_direct(event: Any, message_chain: Any) -> None:
    messages = _build_onebot_messages(message_chain)
    if not messages:
        return

    bot = getattr(event, "bot", None)
    if bot is None:
        raise RuntimeError("当前事件缺少 aiocqhttp bot 实例")

    is_group = bool(event.get_group_id())
    session_id = event.get_group_id() if is_group else event.get_sender_id()
    session_id_int = (
        int(session_id) if session_id and str(session_id).isdigit() else None
    )

    if is_group and isinstance(session_id_int, int):
        await bot.send_group_msg(group_id=session_id_int, message=messages)
        return

    if not is_group and isinstance(session_id_int, int):
        await bot.send_private_msg(user_id=session_id_int, message=messages)
        return

    raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw_event is not None:
        await bot.send(event=raw_event, message=messages)
        return

    raise ValueError(f"无法直传图片：缺少有效会话 ID({session_id}) 和原始事件")


def _build_onebot_messages(message_chain: Any) -> list[dict]:
    messages = []
    for component in getattr(message_chain, "chain", None) or []:
        component_type = _component_type(component)
        if component_type == "Plain" and not getattr(component, "text", "").strip():
            continue

        if isinstance(component, Image):
            messages.append(
                {"type": "image", "data": {"file": _get_remote_image_url(component)}}
            )
            continue

        messages.append(component.toDict())
        if component_type == "At":
            messages.append({"type": "text", "data": {"text": " "}})

    return messages


def _get_remote_image_url(component: Any) -> str:
    if not isinstance(component, Image):
        return ""

    image_url = getattr(component, "url", "") or getattr(component, "file", "")
    if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
        return image_url
    return ""


def _component_type(component: Any) -> str:
    component_type = getattr(component, "type", "")
    if hasattr(component_type, "value"):
        return component_type.value
    return str(component_type)
