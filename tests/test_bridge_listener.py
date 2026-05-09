from __future__ import annotations

from my_wxauto import listener
from my_wxauto.bridge_batcher import BatchingConfig
from my_wxauto.bridge_store import BridgeStore
from my_wxauto.listener import listen_conversation_batches


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
