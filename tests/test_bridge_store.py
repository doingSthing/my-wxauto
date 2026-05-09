from __future__ import annotations

import json
import sqlite3

import pytest

from my_wxauto.bridge_events import BridgeMessage, ConversationBatch
from my_wxauto.bridge_store import BridgeStore


def test_store_records_seen_message_once(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    message = BridgeMessage(chat_name="alice", content="hello", occurrence_index=0).with_key()

    assert store.record_seen_message(message, now=10.0) is True
    assert store.record_seen_message(message, now=12.0) is False
    assert store.is_seen(message.message_key) is True


def test_store_updates_seen_message_last_seen_at(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = BridgeStore(db_path)
    message = BridgeMessage(chat_name="alice", content="hello", occurrence_index=0).with_key()

    store.record_seen_message(message, now=10.0)
    store.record_seen_message(message, now=12.0)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select first_seen_at, last_seen_at from seen_messages where message_key = ?",
            (message.message_key,),
        ).fetchone()

    assert row == (10.0, 12.0)


def test_store_saves_and_updates_batch(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    message = BridgeMessage(chat_name="alice", content="hello", occurrence_index=0).with_key()
    batch = ConversationBatch(
        batch_id="batch-1",
        chat_name="alice",
        messages=(message,),
        created_at=10.0,
        frozen_at=11.0,
        status="frozen",
    )

    store.save_batch(batch)
    store.mark_batch_submitted("batch-1", submitted_at=12.0)
    store.mark_batch_completed("batch-1", completed_at=13.0)

    row = store.get_batch("batch-1")

    assert row is not None
    assert row["status"] == "completed"
    assert row["message_count"] == 1
    assert row["submitted_at"] == 12.0
    assert row["completed_at"] == 13.0


def test_store_keeps_batch_payload_lifecycle_fields_in_sync(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    message = BridgeMessage(chat_name="alice", content="hello", occurrence_index=0).with_key()
    batch = ConversationBatch(
        batch_id="batch-1",
        chat_name="alice",
        messages=(message,),
        created_at=10.0,
        frozen_at=11.0,
        status="frozen",
    )

    store.save_batch(batch)
    store.mark_batch_submitted("batch-1", submitted_at=12.0)
    submitted_row = store.get_batch("batch-1")
    assert submitted_row is not None
    submitted_payload = json.loads(submitted_row["payload_json"])

    assert submitted_payload["status"] == "submitted"
    assert submitted_payload["submitted_at"] == 12.0
    assert submitted_payload["completed_at"] is None

    store.mark_batch_completed("batch-1", completed_at=13.0)
    completed_row = store.get_batch("batch-1")
    assert completed_row is not None
    completed_payload = json.loads(completed_row["payload_json"])

    assert completed_payload["status"] == "completed"
    assert completed_payload["submitted_at"] == 12.0
    assert completed_payload["completed_at"] == 13.0


def test_store_raises_key_error_for_missing_batch_lifecycle_update(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")

    with pytest.raises(KeyError, match="missing-batch"):
        store.mark_batch_submitted("missing-batch", submitted_at=12.0)

    with pytest.raises(KeyError, match="missing-batch"):
        store.mark_batch_completed("missing-batch", completed_at=13.0)


def test_store_matches_outgoing_echo_until_expiry(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")

    store.record_outgoing_echo("alice", "hello", sent_at=10.0, ttl_seconds=30.0)

    assert store.matches_outgoing_echo("alice", "hello", now=20.0) is True
    assert store.matches_outgoing_echo("alice", "hello", now=41.0) is False


def test_store_expires_and_prunes_outgoing_echo_at_expiry_boundary(tmp_path) -> None:
    db_path = tmp_path / "bridge.sqlite3"
    store = BridgeStore(db_path)

    store.record_outgoing_echo("alice", "hello", sent_at=10.0, ttl_seconds=30.0)

    assert store.matches_outgoing_echo("alice", "hello", now=39.999) is True
    assert store.matches_outgoing_echo("alice", "hello", now=40.0) is False

    with sqlite3.connect(db_path) as conn:
        row_count = conn.execute("select count(*) from outgoing_echoes").fetchone()[0]

    assert row_count == 0
