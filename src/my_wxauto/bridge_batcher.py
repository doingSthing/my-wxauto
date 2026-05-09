from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from .bridge_events import BridgeMessage, ConversationBatch
from .bridge_store import BridgeStore


@dataclass(frozen=True)
class BatchingConfig:
    quiet_window_seconds: float = 1.5
    max_batch_wait_seconds: float = 8.0
    max_batch_messages: int = 10


@dataclass(frozen=True)
class _OpenBatch:
    batch_id: str
    chat_name: str
    messages: tuple[BridgeMessage, ...]
    created_at: float
    last_message_at: float

    @property
    def message_count(self) -> int:
        return len(self.messages)


class ConversationBatcher:
    def __init__(self, store: BridgeStore, *, config: BatchingConfig | None = None) -> None:
        self.store = store
        self.config = config or BatchingConfig()
        self._open: dict[str, _OpenBatch] = self._load_open_batches()

    def add_messages(self, chat_name: str, messages: tuple[BridgeMessage, ...], *, now: float) -> int:
        keyed_messages = tuple(message.with_key() for message in messages)
        for keyed in keyed_messages:
            if keyed.chat_name != chat_name:
                raise ValueError(f"message chat_name {keyed.chat_name!r} does not match {chat_name!r}")

        existing = self._open.get(chat_name)
        existing_keys = {message.message_key for message in existing.messages} if existing is not None else set()
        existing_count = existing.message_count if existing is not None else 0
        remaining_capacity = max(self.config.max_batch_messages - existing_count, 0)
        accepted: list[BridgeMessage] = []
        accepted_keys: set[str] = set()
        for keyed in keyed_messages:
            if keyed.is_self is True:
                continue
            if self.store.matches_outgoing_echo(chat_name, keyed.content, now=now):
                self.store.record_seen_message(keyed, now=now)
                continue
            if keyed.message_key in existing_keys:
                self.store.record_seen_message(keyed, now=now)
                continue
            if self.store.is_seen(keyed.message_key):
                continue
            if keyed.message_key in accepted_keys:
                continue
            if len(accepted) >= remaining_capacity:
                break
            accepted.append(keyed)
            accepted_keys.add(keyed.message_key)
        if not accepted:
            return 0

        if existing is None:
            updated = _OpenBatch(
                batch_id=f"wechat-batch-{uuid.uuid4().hex}",
                chat_name=chat_name,
                messages=tuple(accepted),
                created_at=now,
                last_message_at=now,
            )
        else:
            updated = _OpenBatch(
                batch_id=existing.batch_id,
                chat_name=existing.chat_name,
                messages=(*existing.messages, *accepted),
                created_at=existing.created_at,
                last_message_at=now,
            )
        self.store.save_batch(self._to_event(updated, status="open"))
        self._open[chat_name] = updated
        for keyed in accepted:
            self.store.record_seen_message(keyed, now=now)
        return len(accepted)

    def open_batch_for(self, chat_name: str) -> _OpenBatch | None:
        return self._open.get(chat_name)

    def freeze_due_batches(self, *, now: float, limit: int | None = None) -> tuple[ConversationBatch, ...]:
        if limit is not None and limit <= 0:
            return ()
        frozen: list[ConversationBatch] = []
        for chat_name, batch in list(self._open.items()):
            if limit is not None and len(frozen) >= limit:
                break
            if not self._is_due(batch, now=now):
                continue
            event = self._to_event(batch, frozen_at=now, status="frozen")
            self.store.save_batch(event)
            frozen.append(event)
            del self._open[chat_name]
        return tuple(frozen)

    def frozen_batches(self, *, limit: int | None = None) -> tuple[ConversationBatch, ...]:
        if limit is not None and limit <= 0:
            return ()
        rows = self.store.list_batches(status="frozen")
        if limit is not None:
            rows = rows[:limit]
        return tuple(self._batch_from_row(row) for row in rows)

    def _is_due(self, batch: _OpenBatch, *, now: float) -> bool:
        if batch.message_count >= self.config.max_batch_messages:
            return True
        if now - batch.created_at >= self.config.max_batch_wait_seconds:
            return True
        return now - batch.last_message_at >= self.config.quiet_window_seconds

    def _to_event(
        self,
        batch: _OpenBatch,
        *,
        frozen_at: float | None = None,
        status: str,
    ) -> ConversationBatch:
        return ConversationBatch(
            batch_id=batch.batch_id,
            chat_name=batch.chat_name,
            messages=batch.messages,
            created_at=batch.created_at,
            frozen_at=frozen_at,
            status=status,
        )

    def _load_open_batches(self) -> dict[str, _OpenBatch]:
        open_batches: dict[str, _OpenBatch] = {}
        for row in self.store.list_batches(status="open"):
            payload = json.loads(row["payload_json"])
            chat_name = str(payload.get("chat_name") or row["chat_name"])
            created_at = float(payload.get("created_at") or row["created_at"])
            messages = tuple(self._message_from_payload(message) for message in payload.get("messages", ()))
            for message in messages:
                if not self.store.is_seen(message.message_key):
                    self.store.record_seen_message(message, now=created_at)
            # Recovered batches conservatively use created_at for last_message_at because only
            # the stable event payload is persisted.
            open_batches[chat_name] = _OpenBatch(
                batch_id=str(payload.get("batch_id") or row["batch_id"]),
                chat_name=chat_name,
                messages=messages,
                created_at=created_at,
                last_message_at=created_at,
            )
        return open_batches

    def _batch_from_row(self, row: dict[str, Any]) -> ConversationBatch:
        payload = json.loads(row["payload_json"])
        messages = tuple(self._message_from_payload(message) for message in payload.get("messages", ()))
        return ConversationBatch(
            batch_id=str(payload.get("batch_id") or row["batch_id"]),
            chat_name=str(payload.get("chat_name") or row["chat_name"]),
            messages=messages,
            created_at=float(payload.get("created_at") or row["created_at"]),
            frozen_at=_optional_float(payload.get("frozen_at"), row.get("frozen_at")),
            submitted_at=_optional_float(payload.get("submitted_at"), row.get("submitted_at")),
            completed_at=_optional_float(payload.get("completed_at"), row.get("completed_at")),
            status=str(payload.get("status") or row["status"]),
        )

    def _message_from_payload(self, payload: dict[str, Any]) -> BridgeMessage:
        return BridgeMessage(
            chat_name=str(payload.get("chat_name") or ""),
            content=str(payload.get("content") or ""),
            message_type=str(payload.get("message_type") or "unknown"),
            sender=_optional_str(payload.get("sender")),
            is_self=_optional_bool(payload.get("is_self")),
            time_text=_optional_str(payload.get("time_text")),
            occurrence_index=int(payload.get("occurrence_index") or 0),
            message_key=str(payload.get("message_key") or ""),
            raw=payload.get("raw") if isinstance(payload.get("raw"), dict) else None,
        ).with_key()


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


def _optional_float(*values: object) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None
