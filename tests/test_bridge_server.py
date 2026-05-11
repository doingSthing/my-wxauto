from __future__ import annotations

import queue
import threading

import pytest

from my_wxauto.bridge_events import BridgeMessage, ConversationBatch
from my_wxauto.bridge_server import BridgeRuntime, BridgeServerConfig
from my_wxauto.response import WxResponse


class FakeWeChat:
    def __init__(self) -> None:
        self.listen_kwargs: dict[str, object] | None = None
        self.sent: list[tuple[str, str]] = []

    def listen_conversation_batches(self, callback, **kwargs: object):
        self.listen_callback = callback
        self.listen_kwargs = kwargs
        return None

    def SendMsg(self, message: str, who: str):
        self.sent.append((who, message))
        return WxResponse.success("sent", {"who": who, "message": message})


def _batch(content: str = "hello") -> ConversationBatch:
    message = BridgeMessage(
        chat_name="alice",
        content=content,
        message_type="text",
        sender="alice",
        is_self=False,
        time_text="15:41",
    ).with_key()
    return ConversationBatch(
        batch_id=f"batch-{content}",
        chat_name="alice",
        messages=(message,),
        created_at=10.0,
        frozen_at=11.0,
        status="frozen",
    )


def test_runtime_enqueue_and_poll_events() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(queue_size=2), wechat=FakeWeChat())

    runtime.enqueue_batch(_batch("one"))
    runtime.enqueue_batch(_batch("two"))

    payload = runtime.poll_events(timeout=0.0, limit=5)

    assert payload["status"] == "ok"
    assert payload["count"] == 2
    assert [event["messages"][0]["content"] for event in payload["events"]] == ["one", "two"]
    assert runtime.health()["queue_size"] == 0


def test_runtime_poll_events_times_out_empty() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(queue_size=2), wechat=FakeWeChat())

    payload = runtime.poll_events(timeout=0.01, limit=5)

    assert payload == {"status": "ok", "count": 0, "events": []}


def test_runtime_poll_events_clamps_timeout_and_limit() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(queue_size=60), wechat=FakeWeChat())
    for index in range(55):
        runtime.enqueue_batch(_batch(str(index)))

    payload = runtime.poll_events(timeout=-10, limit=1000)

    assert payload["status"] == "ok"
    assert payload["count"] == 50
    assert payload["events"][0]["messages"][0]["content"] == "0"
    assert payload["events"][-1]["messages"][0]["content"] == "49"
    assert runtime.health()["queue_size"] == 5


def test_runtime_enqueue_raises_when_queue_is_full() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(queue_size=1), wechat=FakeWeChat())
    runtime.enqueue_batch(_batch("one"))

    with pytest.raises(queue.Full):
        runtime.enqueue_batch(_batch("two"))


def test_runtime_send_message_uses_ui_lock() -> None:
    events: list[str] = []

    class RecordingLock:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, exc_type, exc, tb):
            events.append("exit")
            return False

    wx = FakeWeChat()
    runtime = BridgeRuntime(BridgeServerConfig(), wechat=wx, ui_lock=RecordingLock())

    response = runtime.send_message("alice", "hello")

    assert response["status"] == "success"
    assert wx.sent == [("alice", "hello")]
    assert events == ["enter", "exit"]


def test_runtime_start_listener_passes_config_and_lock(monkeypatch) -> None:
    wx = FakeWeChat()
    started: list[threading.Thread] = []

    def fake_thread(*, target, daemon):
        thread = threading.Thread(target=lambda: None, daemon=daemon)
        started.append(thread)
        return thread

    monkeypatch.setattr("my_wxauto.bridge_server.threading.Thread", fake_thread)
    config = BridgeServerConfig(
        store_path="bridge.sqlite3",
        max_chats_per_drain=3,
        resolve_senders="profile_card",
        sender_resolve_limit=4,
    )
    runtime = BridgeRuntime(config, wechat=wx)

    runtime.start_listener()
    runtime._listener_target()

    assert len(started) == 1
    assert started[0].daemon is True
    assert getattr(wx.listen_callback, "__self__", None) is runtime
    assert getattr(wx.listen_callback, "__func__", None) is BridgeRuntime.enqueue_batch
    assert wx.listen_kwargs is not None
    assert wx.listen_kwargs["interval"] == 0.25
    assert wx.listen_kwargs["store_path"] == "bridge.sqlite3"
    assert wx.listen_kwargs["max_chats_per_drain"] == 3
    assert wx.listen_kwargs["resolve_senders"] == "profile_card"
    assert wx.listen_kwargs["sender_resolve_limit"] == 4
    assert wx.listen_kwargs["ui_lock"] is runtime.ui_lock


def test_runtime_start_listener_is_concurrently_idempotent(monkeypatch) -> None:
    wx = FakeWeChat()
    runtime = BridgeRuntime(BridgeServerConfig(), wechat=wx)
    entered_first_start = threading.Event()
    release_start = threading.Event()
    start_calls: list[object] = []

    class BlockingThread:
        def __init__(self, *, target, daemon):
            self.daemon = daemon
            self._alive = False

        def is_alive(self) -> bool:
            return self._alive

        def start(self) -> None:
            start_calls.append(self)
            entered_first_start.set()
            release_start.wait(timeout=1)
            self._alive = True

    monkeypatch.setattr("my_wxauto.bridge_server.threading.Thread", BlockingThread)

    first = threading.Thread(target=runtime.start_listener)
    first.start()
    assert entered_first_start.wait(timeout=1)

    second = threading.Thread(target=runtime.start_listener)
    second.start()
    second.join(timeout=0.05)

    assert len(start_calls) == 1

    release_start.set()
    first.join(timeout=1)
    second.join(timeout=1)
    assert not first.is_alive()
    assert not second.is_alive()
