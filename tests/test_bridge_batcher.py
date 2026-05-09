from __future__ import annotations

import json

import pytest

from my_wxauto.bridge_batcher import BatchingConfig, ConversationBatcher
from my_wxauto.bridge_events import BridgeMessage, ConversationBatch
from my_wxauto.bridge_store import BridgeStore


class FailingSaveStore(BridgeStore):
    def save_batch(self, batch: ConversationBatch) -> None:
        raise RuntimeError("save failed")


def _message(chat: str, content: str, index: int = 0, *, is_self: bool | None = False) -> BridgeMessage:
    return BridgeMessage(
        chat_name=chat,
        content=content,
        message_type="text",
        sender="alice",
        is_self=is_self,
        time_text="15:41",
        occurrence_index=index,
    ).with_key()


def test_batcher_deduplicates_repeated_messages(tmp_path) -> None:
    batcher = ConversationBatcher(BridgeStore(tmp_path / "bridge.sqlite3"))
    message = _message("alice", "hello")

    assert batcher.add_messages("alice", (message,), now=10.0) == 1
    assert batcher.add_messages("alice", (message,), now=11.0) == 0

    assert batcher.open_batch_for("alice") is not None
    assert batcher.open_batch_for("alice").message_count == 1


def test_batcher_ignores_self_messages(tmp_path) -> None:
    batcher = ConversationBatcher(BridgeStore(tmp_path / "bridge.sqlite3"))

    added = batcher.add_messages("alice", (_message("alice", "robot", is_self=True),), now=10.0)

    assert added == 0
    assert batcher.open_batch_for("alice") is None


def test_batcher_suppresses_recent_outgoing_echo(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    store.record_outgoing_echo("alice", "hello", sent_at=10.0, ttl_seconds=30.0)
    batcher = ConversationBatcher(store)

    added = batcher.add_messages("alice", (_message("alice", "hello"),), now=20.0)

    assert added == 0
    assert batcher.open_batch_for("alice") is None


def test_batcher_records_echo_suppressed_messages_as_seen(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    message = _message("alice", "hello")
    store.record_outgoing_echo("alice", "hello", sent_at=10.0, ttl_seconds=5.0)
    batcher = ConversationBatcher(store)

    assert batcher.add_messages("alice", (message,), now=12.0) == 0
    assert store.is_seen(message.message_key)

    assert batcher.add_messages("alice", (message,), now=20.0) == 0
    assert batcher.open_batch_for("alice") is None


def test_batcher_does_not_mark_seen_when_open_batch_save_fails(tmp_path) -> None:
    store = FailingSaveStore(tmp_path / "bridge.sqlite3")
    batcher = ConversationBatcher(store)
    message = _message("alice", "hello")

    with pytest.raises(RuntimeError, match="save failed"):
        batcher.add_messages("alice", (message,), now=10.0)

    assert not store.is_seen(message.message_key)
    assert batcher.open_batch_for("alice") is None


def test_batcher_rejects_chat_name_mismatch_before_writing(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    batcher = ConversationBatcher(store)
    message = _message("bob", "wrong chat")

    with pytest.raises(ValueError, match="chat_name"):
        batcher.add_messages("alice", (message,), now=10.0)

    assert not store.is_seen(message.message_key)
    assert batcher.open_batch_for("alice") is None
    assert batcher.open_batch_for("bob") is None


def test_batcher_freezes_by_quiet_window(tmp_path) -> None:
    config = BatchingConfig(quiet_window_seconds=1.5, max_batch_wait_seconds=8.0, max_batch_messages=10)
    batcher = ConversationBatcher(BridgeStore(tmp_path / "bridge.sqlite3"), config=config)

    batcher.add_messages("alice", (_message("alice", "hello"),), now=10.0)

    assert batcher.freeze_due_batches(now=11.0) == ()
    frozen = batcher.freeze_due_batches(now=11.6)

    assert len(frozen) == 1
    assert frozen[0].status == "frozen"
    assert frozen[0].frozen_at == 11.6
    assert batcher.open_batch_for("alice") is None


def test_batcher_freeze_due_batches_limit_keeps_remaining_batches_open(tmp_path) -> None:
    config = BatchingConfig(quiet_window_seconds=1.5, max_batch_wait_seconds=8.0, max_batch_messages=10)
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    batcher = ConversationBatcher(store, config=config)

    batcher.add_messages("alice", (_message("alice", "hello"),), now=10.0)
    batcher.add_messages("bob", (_message("bob", "hi"),), now=10.0)

    frozen = batcher.freeze_due_batches(now=11.6, limit=1)

    assert len(frozen) == 1
    assert batcher.open_batch_for(frozen[0].chat_name) is None
    open_rows = store.list_batches(status="open")
    assert len(open_rows) == 1
    assert open_rows[0]["chat_name"] in {"alice", "bob"} - {frozen[0].chat_name}
    assert batcher.open_batch_for(open_rows[0]["chat_name"]) is not None


def test_batcher_freeze_due_batches_limit_zero_freezes_nothing(tmp_path) -> None:
    config = BatchingConfig(quiet_window_seconds=1.5, max_batch_wait_seconds=8.0, max_batch_messages=10)
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    batcher = ConversationBatcher(store, config=config)

    batcher.add_messages("alice", (_message("alice", "hello"),), now=10.0)

    assert batcher.freeze_due_batches(now=11.6, limit=0) == ()
    assert len(store.list_batches(status="open")) == 1
    assert batcher.open_batch_for("alice") is not None


def test_batcher_freezes_at_exact_quiet_window_boundary(tmp_path) -> None:
    config = BatchingConfig(quiet_window_seconds=1.5, max_batch_wait_seconds=8.0, max_batch_messages=10)
    batcher = ConversationBatcher(BridgeStore(tmp_path / "bridge.sqlite3"), config=config)

    batcher.add_messages("alice", (_message("alice", "hello"),), now=10.0)

    frozen = batcher.freeze_due_batches(now=11.5)

    assert len(frozen) == 1
    assert frozen[0].frozen_at == 11.5


def test_batcher_freezes_by_max_wait_when_chat_stays_busy(tmp_path) -> None:
    config = BatchingConfig(quiet_window_seconds=1.5, max_batch_wait_seconds=8.0, max_batch_messages=10)
    batcher = ConversationBatcher(BridgeStore(tmp_path / "bridge.sqlite3"), config=config)

    batcher.add_messages("alice", (_message("alice", "m1", 0),), now=10.0)
    batcher.add_messages("alice", (_message("alice", "m2", 1),), now=17.9)

    frozen = batcher.freeze_due_batches(now=18.1)

    assert len(frozen) == 1
    assert [message.content for message in frozen[0].messages] == ["m1", "m2"]


def test_batcher_freezes_at_exact_max_wait_boundary(tmp_path) -> None:
    config = BatchingConfig(quiet_window_seconds=1.5, max_batch_wait_seconds=8.0, max_batch_messages=10)
    batcher = ConversationBatcher(BridgeStore(tmp_path / "bridge.sqlite3"), config=config)

    batcher.add_messages("alice", (_message("alice", "m1", 0),), now=10.0)
    batcher.add_messages("alice", (_message("alice", "m2", 1),), now=17.9)

    frozen = batcher.freeze_due_batches(now=18.0)

    assert len(frozen) == 1
    assert [message.content for message in frozen[0].messages] == ["m1", "m2"]


def test_batcher_freezes_by_message_count(tmp_path) -> None:
    config = BatchingConfig(quiet_window_seconds=1.5, max_batch_wait_seconds=8.0, max_batch_messages=2)
    batcher = ConversationBatcher(BridgeStore(tmp_path / "bridge.sqlite3"), config=config)

    accepted = batcher.add_messages(
        "alice",
        (_message("alice", "m1", 0), _message("alice", "m2", 1)),
        now=10.0,
    )

    assert accepted == 2
    due = batcher.freeze_due_batches(now=10.0)
    assert len(due) == 1
    assert due[0].message_count == 2


def test_batcher_does_not_exceed_max_batch_messages(tmp_path) -> None:
    config = BatchingConfig(quiet_window_seconds=1.5, max_batch_wait_seconds=8.0, max_batch_messages=2)
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    batcher = ConversationBatcher(store, config=config)
    messages = (
        _message("alice", "m1", 0),
        _message("alice", "m2", 1),
        _message("alice", "m3", 2),
    )

    accepted = batcher.add_messages("alice", messages, now=10.0)

    assert accepted == 2
    assert batcher.open_batch_for("alice").message_count == 2
    assert [message.content for message in batcher.open_batch_for("alice").messages] == ["m1", "m2"]
    assert not store.is_seen(messages[2].message_key)


def test_batcher_persists_frozen_batch_status_and_payload(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    batcher = ConversationBatcher(store)
    batcher.add_messages("alice", (_message("alice", "hello"),), now=10.0)

    frozen = batcher.freeze_due_batches(now=11.5)
    row = store.get_batch(frozen[0].batch_id)
    payload = json.loads(row["payload_json"])

    assert row["status"] == "frozen"
    assert row["frozen_at"] == 11.5
    assert payload["status"] == "frozen"
    assert payload["message_count"] == 1


def test_batcher_reconstructs_pending_frozen_batches_after_restart(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    first = ConversationBatcher(store)
    message = _message("alice", "hello")
    first.add_messages("alice", (message,), now=10.0)
    frozen = first.freeze_due_batches(now=11.5)

    restarted = ConversationBatcher(store)
    pending = restarted.frozen_batches()

    assert len(pending) == 1
    assert pending[0].batch_id == frozen[0].batch_id
    assert pending[0].status == "frozen"
    assert pending[0].frozen_at == 11.5
    assert pending[0].messages[0].message_key == message.message_key


def test_batcher_frozen_batches_limit_keeps_extra_rows_frozen(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    for chat_name, content in (("alice", "hello"), ("bob", "hi")):
        store.save_batch(
            ConversationBatch(
                batch_id=f"batch-{chat_name}",
                chat_name=chat_name,
                messages=(_message(chat_name, content),),
                created_at=10.0,
                frozen_at=11.5,
                status="frozen",
            )
        )

    pending = ConversationBatcher(store).frozen_batches(limit=1)

    assert len(pending) == 1
    assert len(store.list_batches(status="frozen")) == 2


def test_batcher_recovers_seen_open_batch_and_freezes_after_restart(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    message = _message("alice", "hello")
    first_batcher = ConversationBatcher(store)
    assert first_batcher.add_messages("alice", (message,), now=10.0) == 1

    restarted = ConversationBatcher(store)
    recovered = restarted.open_batch_for("alice")
    assert recovered is not None
    assert recovered.message_count == 1
    assert recovered.messages[0].message_key == message.message_key
    assert recovered.messages[0].content == "hello"
    assert recovered.messages[0].raw == {}

    frozen = restarted.freeze_due_batches(now=18.0)
    row = store.get_batch(frozen[0].batch_id)

    assert len(frozen) == 1
    assert frozen[0].status == "frozen"
    assert row["status"] == "frozen"
    assert restarted.open_batch_for("alice") is None


def test_batcher_recovers_unseen_open_batch_without_duplicating_after_restart(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    message = _message("alice", "hello")
    open_batch = ConversationBatch(
        batch_id="batch-open",
        chat_name="alice",
        messages=(message,),
        created_at=10.0,
        status="open",
    )
    store.save_batch(open_batch)
    assert not store.is_seen(message.message_key)

    restarted = ConversationBatcher(store)
    assert restarted.add_messages("alice", (message,), now=11.0) == 0

    recovered = restarted.open_batch_for("alice")
    assert recovered is not None
    assert recovered.message_count == 1
    assert recovered.messages[0].message_key == message.message_key
    assert recovered.messages[0].content == "hello"
    assert recovered.messages[0].raw == {}
    assert store.is_seen(message.message_key)


def test_batcher_marks_recovered_messages_seen_before_freeze_after_restart(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    message = _message("alice", "hello")
    open_batch = ConversationBatch(
        batch_id="batch-open",
        chat_name="alice",
        messages=(message,),
        created_at=10.0,
        status="open",
    )
    store.save_batch(open_batch)
    assert not store.is_seen(message.message_key)

    restarted = ConversationBatcher(store)
    frozen = restarted.freeze_due_batches(now=18.0)
    accepted = restarted.add_messages("alice", (message,), now=19.0)

    assert len(frozen) == 1
    assert accepted == 0
    assert store.is_seen(message.message_key)
