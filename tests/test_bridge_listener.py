from __future__ import annotations

import json

import pytest

from my_wxauto import listener
from my_wxauto.bridge_batcher import BatchingConfig
from my_wxauto.bridge_events import BridgeMessage, ConversationBatch
from my_wxauto.bridge_store import BridgeStore
from my_wxauto.listener import listen_conversation_batches


def test_resolve_probe_chat_senders_returns_original_payload_when_disabled(monkeypatch) -> None:
    chat = {
        "chat_name": "group",
        "message_region": {"left": 100, "top": 200, "right": 900, "bottom": 700},
        "messages": [
            {
                "content": "hello",
                "message_type": "text",
                "sender": None,
                "rect": {"left": 320, "top": 260, "right": 620, "bottom": 310},
            }
        ],
    }

    def fail_resolver(*_args: object, **_kwargs: object) -> str | None:
        raise AssertionError("sender resolver should not run in default mode")

    monkeypatch.setattr(listener, "_resolve_sender_from_profile_card", fail_resolver)

    assert listener._resolve_probe_chat_senders(chat, resolve_senders=False) is chat


def test_resolve_probe_chat_senders_enriches_sender_before_batching(monkeypatch) -> None:
    chat = {
        "chat_name": "group",
        "message_region": {"left": 100, "top": 200, "right": 900, "bottom": 700},
        "messages": [
            {
                "content": "hello",
                "message_type": "text",
                "sender": None,
                "raw_name": "hello",
                "class_name": "mmui::ChatTextItemView",
                "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
                "rect": {"left": 320, "top": 260, "right": 620, "bottom": 310},
            }
        ],
    }
    resolver_calls: list[str] = []
    progress_events: list[dict[str, object]] = []

    def fake_resolver(message: listener.ChatMessage, **_kwargs: object) -> str | None:
        resolver_calls.append(message.content)
        return "Alice"

    monkeypatch.setattr(listener, "_resolve_sender_from_profile_card", fake_resolver)
    monkeypatch.setattr(
        listener,
        "_annotate_messages_with_self_flags",
        lambda messages, _region: [{**messages[0], "visible_rect": messages[0]["rect"], "is_self": False}],
    )

    enriched = listener._resolve_probe_chat_senders(
        chat,
        resolve_senders="profile_card",
        sender_resolve_limit=5,
        sender_resolve_timeout=20.0,
        profile_card_timeout=2.0,
        sender_progress=progress_events.append,
    )

    assert enriched is not chat
    assert resolver_calls == ["hello"]
    assert enriched["messages"][0]["sender"] == "Alice"
    assert enriched["messages"][0]["is_self"] is False
    assert enriched["messages"][0]["visible_rect"] == {"left": 320, "top": 260, "right": 620, "bottom": 310}
    assert [event["stage"] for event in progress_events] == ["start", "resolved"]


def test_listen_conversation_batches_emits_one_batch_per_chat(monkeypatch, tmp_path) -> None:
    emitted = []

    def fake_icons() -> list[dict[str, object]]:
        return [{"image_sha1": "changed"}]

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def observe(self, _now: float, _signature: object) -> dict[str, object] | None:
            self.calls += 1
            return {"change_count": 1} if self.calls == 1 else None

    def fake_probe(**kwargs: object) -> dict[str, object]:
        assert kwargs["open_unread_messages"] is True
        assert kwargs["max_unread_chats"] == 5
        on_chat_opened = kwargs["on_chat_opened"]
        on_chat_opened(
            {
                "chat_name": "alice",
                "source": "unread_session",
                "messages": [
                    {
                        "content": "hello",
                        "message_type": "text",
                        "sender": "alice",
                        "is_self": False,
                        "time_text": "15:41",
                    }
                ],
            }
        )
        on_chat_opened(
            {
                "chat_name": "bob",
                "source": "unread_session",
                "messages": [
                    {
                        "content": "hi",
                        "message_type": "text",
                        "sender": "bob",
                        "is_self": False,
                        "time_text": "15:42",
                    }
                ],
            }
        )
        return {"status": "ok", "opened_unread_chats": []}

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(listener.probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda icons: tuple(item["image_sha1"] for item in icons))
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    stats = listen_conversation_batches(
        emitted.append,
        seconds=5,
        interval=0.01,
        min_changes=1,
        max_probes=1,
        max_chats_per_drain=5,
        store_path=tmp_path / "bridge.sqlite3",
        batching_config=BatchingConfig(quiet_window_seconds=0.0, max_batch_wait_seconds=8.0, max_batch_messages=10),
    )

    assert stats.event_count == 2
    assert [batch.chat_name for batch in emitted] == ["alice", "bob"]
    assert emitted[0].messages[0].content == "hello"
    assert emitted[1].messages[0].content == "hi"


def test_listen_conversation_batches_default_mode_does_not_resolve_senders(monkeypatch, tmp_path) -> None:
    emitted = []

    def fake_icons() -> list[dict[str, object]]:
        return [{"image_sha1": "changed"}]

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def observe(self, _now: float, _signature: object) -> dict[str, object] | None:
            self.calls += 1
            return {"change_count": 1} if self.calls == 1 else None

    def fake_probe(**kwargs: object) -> dict[str, object]:
        kwargs["on_chat_opened"](
            {
                "chat_name": "group",
                "source": "unread_session",
                "message_region": {"left": 100, "top": 200, "right": 900, "bottom": 700},
                "messages": [
                    {
                        "content": "hello",
                        "message_type": "text",
                        "sender": None,
                        "is_self": None,
                        "time_text": "15:41",
                        "rect": {"left": 320, "top": 260, "right": 620, "bottom": 310},
                    }
                ],
            }
        )
        return {"status": "ok", "opened_unread_chats": []}

    def fail_resolve(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("default listener mode must not resolve senders")

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(listener.probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda icons: tuple(item["image_sha1"] for item in icons))
    monkeypatch.setattr(listener, "_resolve_probe_chat_senders", fail_resolve)
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    stats = listen_conversation_batches(
        emitted.append,
        seconds=5,
        interval=0.01,
        min_changes=1,
        max_probes=1,
        store_path=tmp_path / "bridge.sqlite3",
        batching_config=BatchingConfig(quiet_window_seconds=0.0, max_batch_wait_seconds=8.0, max_batch_messages=10),
    )

    assert stats.event_count == 1
    assert emitted[0].messages[0].sender is None
    assert emitted[0].messages[0].is_self is None


def test_listen_conversation_batches_resolves_senders_when_enabled(monkeypatch, tmp_path) -> None:
    emitted = []
    progress_events: list[dict[str, object]] = []
    sender_progress = progress_events.append

    def fake_icons() -> list[dict[str, object]]:
        return [{"image_sha1": "changed"}]

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def observe(self, _now: float, _signature: object) -> dict[str, object] | None:
            self.calls += 1
            return {"change_count": 1} if self.calls == 1 else None

    def fake_probe(**kwargs: object) -> dict[str, object]:
        assert kwargs["max_unread_chats"] == 1
        kwargs["on_chat_opened"](
            {
                "chat_name": "group",
                "source": "unread_session",
                "message_region": {"left": 100, "top": 200, "right": 900, "bottom": 700},
                "messages": [
                    {
                        "content": "hello",
                        "message_type": "text",
                        "sender": None,
                        "is_self": None,
                        "time_text": "15:41",
                        "rect": {"left": 320, "top": 260, "right": 620, "bottom": 310},
                    }
                ],
            }
        )
        return {"status": "ok", "opened_unread_chats": []}

    def fake_resolve(chat: dict[str, object], **kwargs: object) -> dict[str, object]:
        assert kwargs["resolve_senders"] == "profile_card"
        assert kwargs["sender_resolve_limit"] == 2
        assert kwargs["sender_resolve_timeout"] == 7.0
        assert kwargs["profile_card_timeout"] == 1.0
        assert kwargs["sender_progress"] is sender_progress
        message = dict(chat["messages"][0])
        message["sender"] = "Alice"
        message["is_self"] = False
        return {**chat, "messages": [message]}

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(listener.probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda icons: tuple(item["image_sha1"] for item in icons))
    monkeypatch.setattr(listener, "_resolve_probe_chat_senders", fake_resolve)
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    stats = listen_conversation_batches(
        emitted.append,
        seconds=5,
        interval=0.01,
        min_changes=1,
        max_probes=1,
        max_chats_per_drain=5,
        resolve_senders="profile_card",
        sender_resolve_limit=2,
        sender_resolve_timeout=7.0,
        profile_card_timeout=1.0,
        sender_progress=sender_progress,
        store_path=tmp_path / "bridge.sqlite3",
        batching_config=BatchingConfig(quiet_window_seconds=0.0, max_batch_wait_seconds=8.0, max_batch_messages=10),
    )

    assert stats.event_count == 1
    assert emitted[0].messages[0].sender == "Alice"
    assert emitted[0].messages[0].is_self is False


def test_resolve_probe_chat_senders_only_resolves_unread_suffix(monkeypatch) -> None:
    chat = {
        "chat_name": "group",
        "unread_count": 1,
        "message_region": {"left": 100, "top": 200, "right": 900, "bottom": 700},
        "messages": [
            {
                "content": "old one",
                "message_type": "text",
                "sender": None,
                "raw_name": "old one",
                "class_name": "mmui::ChatTextItemView",
                "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
                "rect": {"left": 320, "top": 220, "right": 620, "bottom": 260},
            },
            {
                "content": "old two",
                "message_type": "text",
                "sender": None,
                "raw_name": "old two",
                "class_name": "mmui::ChatTextItemView",
                "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
                "rect": {"left": 320, "top": 280, "right": 620, "bottom": 320},
            },
            {
                "content": "new message",
                "message_type": "text",
                "sender": None,
                "raw_name": "new message",
                "class_name": "mmui::ChatTextItemView",
                "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
                "rect": {"left": 320, "top": 340, "right": 620, "bottom": 380},
            },
        ],
    }
    resolver_calls: list[str] = []

    def fake_resolver(message: listener.ChatMessage, **_kwargs: object) -> str | None:
        resolver_calls.append(message.content)
        return f"{message.content}-sender"

    monkeypatch.setattr(listener, "_resolve_sender_from_profile_card", fake_resolver)
    monkeypatch.setattr(
        listener,
        "_annotate_messages_with_self_flags",
        lambda messages, _region: [
            {**message, "visible_rect": message["rect"], "is_self": False}
            for message in messages
        ],
    )

    enriched = listener._resolve_probe_chat_senders(
        chat,
        resolve_senders="profile_card",
        sender_resolve_limit=5,
        sender_resolve_timeout=20.0,
        profile_card_timeout=2.0,
    )

    assert resolver_calls == ["new message"]
    assert [message["sender"] for message in enriched["messages"]] == [None, None, "new message-sender"]


def test_listen_conversation_batches_deduplicates_repeated_probe_messages(monkeypatch, tmp_path) -> None:
    emitted = []

    def fake_icons() -> list[dict[str, object]]:
        return [{"image_sha1": "changed"}]

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def observe(self, _now: float, _signature: object) -> dict[str, object] | None:
            self.calls += 1
            return {"change_count": self.calls} if self.calls <= 2 else None

    def fake_probe(**kwargs: object) -> dict[str, object]:
        kwargs["on_chat_opened"](
            {
                "chat_name": "alice",
                "source": "unread_session",
                "messages": [
                    {
                        "content": "hello",
                        "message_type": "text",
                        "sender": "alice",
                        "is_self": False,
                        "time_text": "15:41",
                    }
                ],
            }
        )
        return {"status": "ok", "opened_unread_chats": []}

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(listener.probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda icons: tuple(item["image_sha1"] for item in icons))
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    stats = listen_conversation_batches(
        emitted.append,
        seconds=5,
        interval=0.01,
        min_changes=1,
        max_probes=2,
        store_path=tmp_path / "bridge.sqlite3",
        batching_config=BatchingConfig(quiet_window_seconds=0.0, max_batch_wait_seconds=8.0, max_batch_messages=10),
    )

    assert stats.event_count == 1
    assert len(emitted) == 1


def test_listen_conversation_batches_event_budget_keeps_unemitted_due_batch_open(monkeypatch, tmp_path) -> None:
    emitted = []
    store_path = tmp_path / "bridge.sqlite3"

    def fake_icons() -> list[dict[str, object]]:
        return [{"image_sha1": "changed"}]

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def observe(self, _now: float, _signature: object) -> dict[str, object] | None:
            self.calls += 1
            return {"change_count": 1} if self.calls == 1 else None

    def fake_probe(**kwargs: object) -> dict[str, object]:
        on_chat_opened = kwargs["on_chat_opened"]
        for chat_name, content in (("alice", "hello"), ("bob", "hi")):
            on_chat_opened(
                {
                    "chat_name": chat_name,
                    "source": "unread_session",
                    "messages": [
                        {
                            "content": content,
                            "message_type": "text",
                            "sender": chat_name,
                            "is_self": False,
                            "time_text": "15:41",
                        }
                    ],
                }
            )
        return {"status": "ok", "opened_unread_chats": []}

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(listener.probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda icons: tuple(item["image_sha1"] for item in icons))
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    stats = listen_conversation_batches(
        emitted.append,
        seconds=5,
        interval=0.01,
        min_changes=1,
        max_events=1,
        max_probes=1,
        store_path=store_path,
        batching_config=BatchingConfig(quiet_window_seconds=0.0, max_batch_wait_seconds=8.0, max_batch_messages=10),
    )

    open_rows = BridgeStore(store_path).list_batches(status="open")
    assert stats.event_count == 1
    assert stats.stopped_reason == "max_events"
    assert len(emitted) == 1
    assert len(open_rows) == 1
    assert open_rows[0]["chat_name"] in {"alice", "bob"} - {emitted[0].chat_name}


def test_listen_conversation_batches_max_probes_can_collect_without_immediate_emit(monkeypatch, tmp_path) -> None:
    emitted = []
    store_path = tmp_path / "bridge.sqlite3"

    def fake_icons() -> list[dict[str, object]]:
        return [{"image_sha1": "changed"}]

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def observe(self, _now: float, _signature: object) -> dict[str, object] | None:
            self.calls += 1
            return {"change_count": 1} if self.calls == 1 else None

    def fake_probe(**kwargs: object) -> dict[str, object]:
        kwargs["on_chat_opened"](
            {
                "chat_name": "alice",
                "source": "unread_session",
                "messages": [
                    {
                        "content": "hello",
                        "message_type": "text",
                        "sender": "alice",
                        "is_self": False,
                        "time_text": "15:41",
                    }
                ],
            }
        )
        return {"status": "ok", "opened_unread_chats": []}

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(listener.probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda icons: tuple(item["image_sha1"] for item in icons))
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    stats = listen_conversation_batches(
        emitted.append,
        seconds=5,
        interval=0.01,
        min_changes=1,
        max_probes=1,
        store_path=store_path,
    )

    assert stats.event_count == 0
    assert stats.stopped_reason == "max_probes"
    assert emitted == []
    assert len(BridgeStore(store_path).list_batches(status="open")) == 1


def test_listen_conversation_batches_flushes_due_batch_on_interval_tick(monkeypatch, tmp_path) -> None:
    emitted = []
    monotonic_values = iter([0.0, 0.0, 0.0, 0.5, 1.1, 1.1])

    def fake_monotonic() -> float:
        return next(monotonic_values, 1.1)

    def fake_icons() -> list[dict[str, object]]:
        return [{"image_sha1": "changed"}]

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def observe(self, _now: float, _signature: object) -> dict[str, object] | None:
            self.calls += 1
            return {"change_count": 1} if self.calls == 1 else None

    def fake_probe(**kwargs: object) -> dict[str, object]:
        kwargs["on_chat_opened"](
            {
                "chat_name": "alice",
                "source": "unread_session",
                "messages": [
                    {
                        "content": "hello",
                        "message_type": "text",
                        "sender": "alice",
                        "is_self": False,
                        "time_text": "15:41",
                    }
                ],
            }
        )
        assert emitted == []
        return {"status": "ok", "opened_unread_chats": []}

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(listener.probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda icons: tuple(item["image_sha1"] for item in icons))
    monkeypatch.setattr(listener.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    stats = listen_conversation_batches(
        emitted.append,
        seconds=0,
        interval=0.01,
        min_changes=1,
        max_events=1,
        store_path=tmp_path / "bridge.sqlite3",
        batching_config=BatchingConfig(quiet_window_seconds=1.0, max_batch_wait_seconds=8.0, max_batch_messages=10),
    )

    assert stats.event_count == 1
    assert stats.stopped_reason == "max_events"
    assert [batch.chat_name for batch in emitted] == ["alice"]


def test_listen_conversation_batches_retries_frozen_batch_after_callback_failure(monkeypatch, tmp_path) -> None:
    store_path = tmp_path / "bridge.sqlite3"
    message = BridgeMessage(
        chat_name="alice",
        content="hello",
        message_type="text",
        sender="alice",
        is_self=False,
        time_text="15:41",
    ).with_key()
    batch = ConversationBatch(
        batch_id="batch-frozen",
        chat_name="alice",
        messages=(message,),
        created_at=100.0,
        frozen_at=101.0,
        status="frozen",
    )
    BridgeStore(store_path).save_batch(batch)

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def observe(self, _now: float, _signature: object) -> None:
            return None

    monotonic_values = iter([0.0, 0.0, 0.0])

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", lambda: [])
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda _icons: ())
    monkeypatch.setattr(listener.time, "monotonic", lambda: next(monotonic_values, 2.0))
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    def failing_callback(_batch: ConversationBatch) -> None:
        raise RuntimeError("handoff failed")

    with pytest.raises(RuntimeError, match="handoff failed"):
        listen_conversation_batches(
            failing_callback,
            seconds=1,
            interval=0.01,
            max_events=1,
            store_path=store_path,
        )

    assert BridgeStore(store_path).get_batch("batch-frozen")["status"] == "frozen"

    delivered = []
    monotonic_values = iter([0.0, 0.0, 0.0])

    stats = listen_conversation_batches(
        delivered.append,
        seconds=1,
        interval=0.01,
        max_events=1,
        store_path=store_path,
    )

    row = BridgeStore(store_path).get_batch("batch-frozen")
    assert stats.event_count == 1
    assert delivered[0].batch_id == "batch-frozen"
    assert row["status"] == "submitted"
    assert row["submitted_at"] is not None


def test_listen_conversation_batches_respects_budget_for_existing_frozen_batches(monkeypatch, tmp_path) -> None:
    store_path = tmp_path / "bridge.sqlite3"
    store = BridgeStore(store_path)
    for chat_name, content in (("alice", "hello"), ("bob", "hi")):
        message = BridgeMessage(chat_name=chat_name, content=content).with_key()
        store.save_batch(
            ConversationBatch(
                batch_id=f"batch-{chat_name}",
                chat_name=chat_name,
                messages=(message,),
                created_at=100.0,
                frozen_at=101.0,
                status="frozen",
            )
        )

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def observe(self, _now: float, _signature: object) -> None:
            return None

    monotonic_values = iter([0.0, 0.0, 0.0])
    delivered = []

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", lambda: [])
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda _icons: ())
    monkeypatch.setattr(listener.time, "monotonic", lambda: next(monotonic_values, 2.0))
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    stats = listen_conversation_batches(
        delivered.append,
        seconds=1,
        interval=0.01,
        max_events=1,
        store_path=store_path,
    )

    rows = BridgeStore(store_path).list_batches()
    statuses = {row["batch_id"]: row["status"] for row in rows}
    assert stats.event_count == 1
    assert len(delivered) == 1
    assert statuses[delivered[0].batch_id] == "submitted"
    assert list(statuses.values()).count("frozen") == 1


def test_listen_conversation_batches_persists_wall_clock_batch_timestamps(monkeypatch, tmp_path) -> None:
    emitted = []
    store_path = tmp_path / "bridge.sqlite3"
    wall_clock = 1_700_000_000.0

    def fake_icons() -> list[dict[str, object]]:
        return [{"image_sha1": "changed"}]

    class FakeDetector:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def observe(self, _now: float, _signature: object) -> dict[str, object] | None:
            self.calls += 1
            return {"change_count": 1} if self.calls == 1 else None

    def fake_probe(**kwargs: object) -> dict[str, object]:
        kwargs["on_chat_opened"](
            {
                "chat_name": "alice",
                "source": "unread_session",
                "messages": [
                    {
                        "content": "hello",
                        "message_type": "text",
                        "sender": "alice",
                        "is_self": False,
                        "time_text": "15:41",
                    }
                ],
            }
        )
        return {"status": "ok", "opened_unread_chats": []}

    monkeypatch.setattr(listener.probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(listener.probes, "TaskbarFlashDetector", FakeDetector)
    monkeypatch.setattr(listener.probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(listener.probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)
    monkeypatch.setattr(listener.probes, "_taskbar_signature", lambda icons: tuple(item["image_sha1"] for item in icons))
    monkeypatch.setattr(listener.time, "monotonic", lambda: 42.0)
    monkeypatch.setattr(listener.time, "time", lambda: wall_clock)
    monkeypatch.setattr(listener.time, "sleep", lambda _seconds: None)

    stats = listen_conversation_batches(
        emitted.append,
        seconds=5,
        interval=0.01,
        min_changes=1,
        max_events=1,
        store_path=store_path,
        batching_config=BatchingConfig(quiet_window_seconds=0.0, max_batch_wait_seconds=8.0, max_batch_messages=10),
    )

    row = BridgeStore(store_path).get_batch(emitted[0].batch_id)
    payload = json.loads(row["payload_json"])
    assert stats.event_count == 1
    assert row["created_at"] == wall_clock
    assert row["frozen_at"] == wall_clock
    assert row["submitted_at"] == wall_clock
    assert payload["created_at"] == wall_clock
    assert payload["frozen_at"] == wall_clock
    assert payload["submitted_at"] == wall_clock
