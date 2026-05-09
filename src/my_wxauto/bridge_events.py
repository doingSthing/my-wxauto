from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


PLATFORM = "wechat_desktop"


@dataclass(frozen=True)
class BridgeMessage:
    chat_name: str
    content: str
    message_type: str = "unknown"
    sender: str | None = None
    is_self: bool | None = None
    time_text: str | None = None
    occurrence_index: int = 0
    message_key: str = ""
    raw: dict[str, Any] | None = None

    def with_key(self) -> "BridgeMessage":
        if self.message_key:
            return self
        return BridgeMessage(
            chat_name=self.chat_name,
            content=self.content,
            message_type=self.message_type,
            sender=self.sender,
            is_self=self.is_self,
            time_text=self.time_text,
            occurrence_index=self.occurrence_index,
            message_key=make_message_key(self),
            raw=self.raw,
        )

    def to_dict(self) -> dict[str, Any]:
        keyed = self.with_key()
        return {
            "message_key": keyed.message_key,
            "chat_name": keyed.chat_name,
            "sender": keyed.sender,
            "is_self": keyed.is_self,
            "message_type": keyed.message_type,
            "content": keyed.content,
            "time_text": keyed.time_text,
            "occurrence_index": keyed.occurrence_index,
            "raw": keyed.raw or {},
        }


@dataclass(frozen=True)
class ConversationBatch:
    batch_id: str
    chat_name: str
    messages: tuple[BridgeMessage, ...]
    created_at: float
    frozen_at: float | None = None
    submitted_at: float | None = None
    completed_at: float | None = None
    status: str = "open"

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def to_event_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.batch_id,
            "batch_id": self.batch_id,
            "platform": PLATFORM,
            "chat_id": f"wechat:{self.chat_name}",
            "chat_name": self.chat_name,
            "status": self.status,
            "created_at": self.created_at,
            "frozen_at": self.frozen_at,
            "submitted_at": self.submitted_at,
            "completed_at": self.completed_at,
            "message_count": self.message_count,
            "messages": [message.to_dict() for message in self.messages],
        }


def make_message_key(message: BridgeMessage) -> str:
    payload = {
        "chat_name": message.chat_name,
        "sender": message.sender,
        "is_self": message.is_self,
        "message_type": message.message_type,
        "content": message.content,
        "time_text": message.time_text,
        "occurrence_index": message.occurrence_index,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def messages_from_chat_payload(chat: dict[str, Any]) -> tuple[BridgeMessage, ...]:
    chat_name = str(chat.get("chat_name") or "")
    result: list[BridgeMessage] = []
    occurrences: dict[tuple[object, ...], int] = {}
    for payload in chat.get("messages") or []:
        if not isinstance(payload, dict):
            continue
        content_value = payload.get("content")
        if not _optional_str(content_value):
            content_value = payload.get("raw_name")
        content = str(content_value or "")
        if not content.strip():
            continue
        message_type = str(payload.get("message_type") or "unknown")
        sender = _optional_str(payload.get("sender"))
        is_self = _optional_bool(payload.get("is_self"))
        time_text = _optional_str(payload.get("time_text"))
        fingerprint = (chat_name, sender, is_self, message_type, content, time_text)
        occurrence_index = occurrences.get(fingerprint, 0)
        occurrences[fingerprint] = occurrence_index + 1
        message = BridgeMessage(
            chat_name=chat_name,
            content=content,
            message_type=message_type,
            sender=sender,
            is_self=is_self,
            time_text=time_text,
            occurrence_index=occurrence_index,
            raw=payload,
        ).with_key()
        result.append(message)
    return tuple(result)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None
