from __future__ import annotations

import hashlib
from typing import Any


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def session_name_for_chat(chat_name: Any) -> str:
    normalized_chat_name = _normalize_text(chat_name)
    digest = hashlib.sha1(normalized_chat_name.encode("utf-8")).hexdigest()[:16]
    return f"wxauto-{digest}"


def format_prompt(event: dict[str, Any]) -> str:
    chat_name = _normalize_text(event.get("chat_name"))
    lines = [
        "你正在作为微信机器人回复一个会话。",
        f"会话名：{chat_name}",
        "",
        "本次收到的新消息：",
    ]

    for message in event.get("messages", ()):
        if not isinstance(message, dict):
            continue

        content_value = message.get("content")
        if content_value is None:
            continue

        content = str(content_value).strip()
        if not content:
            continue

        sender = _normalize_text(message.get("sender")).strip()
        if not sender:
            sender = "我" if message.get("is_self") is True else "对方"

        time_text = _normalize_text(message.get("time_text")).strip()
        if time_text:
            lines.append(f"- {time_text} {sender}: {content}")
        else:
            lines.append(f"- {sender}: {content}")

    lines.append("")
    lines.append("请只输出要发送到微信的回复文本。不要解释，不要包含前后缀。")
    return "\n".join(lines)
