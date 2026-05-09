from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .bridge_events import BridgeMessage, ConversationBatch


class BridgeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def record_seen_message(self, message: BridgeMessage, *, now: float | None = None) -> bool:
        keyed = message.with_key()
        timestamp = _now(now)
        payload = json.dumps(keyed.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._connection() as conn:
            try:
                conn.execute(
                    """
                    insert into seen_messages(message_key, chat_name, first_seen_at, last_seen_at, payload_json)
                    values (?, ?, ?, ?, ?)
                    """,
                    (keyed.message_key, keyed.chat_name, timestamp, timestamp, payload),
                )
                return True
            except sqlite3.IntegrityError:
                conn.execute(
                    "update seen_messages set last_seen_at = ? where message_key = ?",
                    (timestamp, keyed.message_key),
                )
                return False

    def is_seen(self, message_key: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "select 1 from seen_messages where message_key = ?",
                (message_key,),
            ).fetchone()
        return row is not None

    def save_batch(self, batch: ConversationBatch) -> None:
        payload = json.dumps(batch.to_event_dict(), ensure_ascii=False, sort_keys=True)
        with self._connection() as conn:
            conn.execute(
                """
                insert or replace into conversation_batches(
                    batch_id, chat_name, status, created_at, frozen_at,
                    submitted_at, completed_at, message_count, payload_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.batch_id,
                    batch.chat_name,
                    batch.status,
                    batch.created_at,
                    batch.frozen_at,
                    batch.submitted_at,
                    batch.completed_at,
                    batch.message_count,
                    payload,
                ),
            )

    def mark_batch_submitted(self, batch_id: str, *, submitted_at: float | None = None) -> None:
        timestamp = _now(submitted_at)
        with self._connection() as conn:
            payload = self._updated_batch_payload(
                conn,
                batch_id,
                status="submitted",
                submitted_at=timestamp,
            )
            conn.execute(
                """
                update conversation_batches
                set status = ?, submitted_at = ?, payload_json = ?
                where batch_id = ?
                """,
                ("submitted", timestamp, payload, batch_id),
            )

    def mark_batch_completed(self, batch_id: str, *, completed_at: float | None = None) -> None:
        timestamp = _now(completed_at)
        with self._connection() as conn:
            payload = self._updated_batch_payload(
                conn,
                batch_id,
                status="completed",
                completed_at=timestamp,
            )
            conn.execute(
                """
                update conversation_batches
                set status = ?, completed_at = ?, payload_json = ?
                where batch_id = ?
                """,
                ("completed", timestamp, payload, batch_id),
            )

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                select batch_id, chat_name, status, created_at, frozen_at,
                       submitted_at, completed_at, message_count, payload_json
                from conversation_batches
                where batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_outgoing_echo(
        self,
        chat_name: str,
        content: str,
        *,
        sent_at: float | None = None,
        ttl_seconds: float = 300.0,
    ) -> str:
        timestamp = _now(sent_at)
        echo_key = _echo_key(chat_name, content)
        with self._connection() as conn:
            conn.execute(
                """
                insert or replace into outgoing_echoes(echo_key, chat_name, content, sent_at, expires_at)
                values (?, ?, ?, ?, ?)
                """,
                (echo_key, chat_name, content, timestamp, timestamp + ttl_seconds),
            )
        return echo_key

    def matches_outgoing_echo(self, chat_name: str, content: str, *, now: float | None = None) -> bool:
        timestamp = _now(now)
        self.prune_expired_echoes(now=timestamp)
        with self._connection() as conn:
            row = conn.execute(
                """
                select 1 from outgoing_echoes
                where echo_key = ? and expires_at > ?
                """,
                (_echo_key(chat_name, content), timestamp),
            ).fetchone()
        return row is not None

    def prune_expired_echoes(self, *, now: float | None = None) -> int:
        timestamp = _now(now)
        with self._connection() as conn:
            cursor = conn.execute(
                "delete from outgoing_echoes where expires_at <= ?",
                (timestamp,),
            )
            return int(cursor.rowcount or 0)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                create table if not exists seen_messages (
                    message_key text primary key,
                    chat_name text not null,
                    first_seen_at real not null,
                    last_seen_at real not null,
                    payload_json text not null
                );

                create table if not exists conversation_batches (
                    batch_id text primary key,
                    chat_name text not null,
                    status text not null,
                    created_at real not null,
                    frozen_at real,
                    submitted_at real,
                    completed_at real,
                    message_count integer not null,
                    payload_json text not null
                );

                create table if not exists outgoing_echoes (
                    echo_key text primary key,
                    chat_name text not null,
                    content text not null,
                    sent_at real not null,
                    expires_at real not null
                );
                """
            )

    def _updated_batch_payload(
        self,
        conn: sqlite3.Connection,
        batch_id: str,
        *,
        status: str,
        submitted_at: float | None = None,
        completed_at: float | None = None,
    ) -> str:
        row = conn.execute(
            """
            select payload_json
            from conversation_batches
            where batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"conversation batch not found: {batch_id}")

        payload = json.loads(row["payload_json"])
        payload["status"] = status
        if submitted_at is not None:
            payload["submitted_at"] = submitted_at
        if completed_at is not None:
            payload["completed_at"] = completed_at
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _echo_key(chat_name: str, content: str) -> str:
    raw = json.dumps({"chat_name": chat_name, "content": content}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now(value: float | None) -> float:
    return time.time() if value is None else float(value)
