from __future__ import annotations

import http.client
import json
import queue
import socket
import threading

import pytest

from my_wxauto.bridge_events import BridgeMessage, ConversationBatch
from my_wxauto.bridge_server import (
    BridgeHTTPServer,
    BridgeRequestHandler,
    BridgeRuntime,
    BridgeServerConfig,
    create_bridge_server,
    run_bridge_server,
)
from my_wxauto.bridge_store import BridgeStore
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


class RunningServer:
    def __init__(self, runtime: BridgeRuntime) -> None:
        self.server = BridgeHTTPServer(("127.0.0.1", 0), BridgeRequestHandler, runtime=runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        return False

    @property
    def host(self) -> str:
        return self.server.server_address[0]

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def request(self, method: str, path: str, body: object | str | None = None):
        headers = {}
        encoded_body = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            encoded_body = body if isinstance(body, str) else json.dumps(body)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=2)
        try:
            connection.request(method, path, body=encoded_body, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, response.getheader("Content-Type"), payload
        finally:
            connection.close()


def _raw_http_request(host: str, port: int, request: str) -> tuple[int, dict[str, str], dict[str, object]]:
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(request.encode("ascii"))
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)

    response = b"".join(chunks)
    header_block, body = response.split(b"\r\n\r\n", 1)
    header_lines = header_block.decode("iso-8859-1").split("\r\n")
    status = int(header_lines[0].split()[1])
    headers = {}
    for line in header_lines[1:]:
        key, value = line.split(":", 1)
        headers[key.lower()] = value.strip()
    return status, headers, json.loads(body.decode("utf-8"))


def test_bridge_http_server_uses_daemon_request_threads() -> None:
    assert BridgeHTTPServer.daemon_threads is True


def test_http_health_endpoint() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(store_path="bridge.sqlite3"), wechat=FakeWeChat())

    with RunningServer(runtime) as server:
        status, content_type, payload = server.request("GET", "/health")

    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    assert payload == {
        "status": "ok",
        "queue_size": 0,
        "listener_alive": False,
        "store_path": "bridge.sqlite3",
    }


def test_http_events_endpoint_returns_queued_events() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(queue_size=2), wechat=FakeWeChat())
    runtime.enqueue_batch(_batch("one"))
    runtime.enqueue_batch(_batch("two"))

    with RunningServer(runtime) as server:
        status, content_type, payload = server.request("GET", "/events?timeout=0&limit=1")

    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["events"][0]["messages"][0]["content"] == "one"
    assert runtime.health()["queue_size"] == 1


def test_http_events_endpoint_times_out_empty(tmp_path) -> None:
    runtime = BridgeRuntime(BridgeServerConfig(store_path=tmp_path / "bridge.sqlite3"), wechat=FakeWeChat())

    with RunningServer(runtime) as server:
        status, content_type, payload = server.request("GET", "/events?timeout=0.01&limit=5")

    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    assert payload == {"status": "ok", "count": 0, "events": []}


def test_http_send_endpoint() -> None:
    wx = FakeWeChat()
    runtime = BridgeRuntime(BridgeServerConfig(), wechat=wx)

    with RunningServer(runtime) as server:
        status, content_type, payload = server.request(
            "POST",
            "/send",
            {"who": "alice", "message": "hello"},
        )

    assert status == 200
    assert content_type == "application/json; charset=utf-8"
    assert payload["status"] == "success"
    assert payload["data"] == {"who": "alice", "message": "hello"}
    assert wx.sent == [("alice", "hello")]


def test_http_send_rejects_invalid_json() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(), wechat=FakeWeChat())

    with RunningServer(runtime) as server:
        status, content_type, payload = server.request("POST", "/send", "{")

    assert status == 400
    assert content_type == "application/json; charset=utf-8"
    assert payload["status"] == "error"
    assert "Invalid JSON" in payload["message"]


def test_http_send_rejects_non_integer_content_length() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(), wechat=FakeWeChat())

    with RunningServer(runtime) as server:
        status, headers, payload = _raw_http_request(
            server.host,
            server.port,
            "POST /send HTTP/1.1\r\n"
            f"Host: {server.host}:{server.port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: abc\r\n"
            "\r\n",
        )

    assert status == 400
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert payload == {"status": "error", "message": "Invalid Content-Length"}


def test_http_send_rejects_negative_content_length() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(), wechat=FakeWeChat())

    with RunningServer(runtime) as server:
        status, headers, payload = _raw_http_request(
            server.host,
            server.port,
            "POST /send HTTP/1.1\r\n"
            f"Host: {server.host}:{server.port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: -1\r\n"
            "\r\n",
        )

    assert status == 400
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert payload == {"status": "error", "message": "Invalid Content-Length"}


def test_http_send_rejects_too_large_content_length() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(), wechat=FakeWeChat())

    with RunningServer(runtime) as server:
        status, headers, payload = _raw_http_request(
            server.host,
            server.port,
            "POST /send HTTP/1.1\r\n"
            f"Host: {server.host}:{server.port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 1048577\r\n"
            "\r\n",
        )

    assert status == 413
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert payload == {"status": "error", "message": "JSON body too large"}


@pytest.mark.parametrize(
    "body",
    [
        {"message": "hello"},
        {"who": "alice"},
        {"who": "", "message": "hello"},
        {"who": "alice", "message": ""},
    ],
)
def test_http_send_rejects_missing_who_or_message(body: dict[str, str]) -> None:
    runtime = BridgeRuntime(BridgeServerConfig(), wechat=FakeWeChat())

    with RunningServer(runtime) as server:
        status, content_type, payload = server.request("POST", "/send", body)

    assert status == 400
    assert content_type == "application/json; charset=utf-8"
    assert payload == {"status": "error", "message": "who and message are required"}


def test_http_unknown_route_returns_404() -> None:
    runtime = BridgeRuntime(BridgeServerConfig(), wechat=FakeWeChat())

    with RunningServer(runtime) as server:
        status, content_type, payload = server.request("GET", "/missing")

    assert status == 404
    assert content_type == "application/json; charset=utf-8"
    assert payload == {"status": "error", "message": "not found"}


def test_create_bridge_server_starts_listener_and_returns_configured_server() -> None:
    class BlockingWeChat(FakeWeChat):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def listen_conversation_batches(self, callback, **kwargs: object):
            self.started.set()
            self.release.wait(timeout=1)
            return super().listen_conversation_batches(callback, **kwargs)

    wx = BlockingWeChat()
    server = create_bridge_server(BridgeServerConfig(port=0), wechat=wx)
    try:
        assert wx.started.wait(timeout=1)
        assert isinstance(server, BridgeHTTPServer)
        assert server.runtime.health()["listener_alive"] is True
        assert server.server_address[0] == "127.0.0.1"
    finally:
        wx.release.set()
        server.server_close()


def test_create_bridge_server_does_not_start_listener_when_server_construction_fails(monkeypatch) -> None:
    events: list[str] = []

    class FakeRuntime:
        def __init__(self, config, **kwargs):
            events.append("runtime")

        def start_listener(self):
            events.append("start_listener")

    class FailingServer:
        def __init__(self, address, handler, *, runtime):
            events.append(f"server:{address[0]}:{address[1]}")
            raise OSError("port in use")

    monkeypatch.setattr("my_wxauto.bridge_server.BridgeRuntime", FakeRuntime)
    monkeypatch.setattr("my_wxauto.bridge_server.BridgeHTTPServer", FailingServer)

    with pytest.raises(OSError, match="port in use"):
        create_bridge_server(BridgeServerConfig(port=0))

    assert events == ["runtime", "server:127.0.0.1:0"]


def test_create_bridge_server_closes_server_when_start_listener_fails(monkeypatch) -> None:
    events: list[str] = []

    class FakeRuntime:
        def __init__(self, config, **kwargs):
            events.append("runtime")

        def start_listener(self):
            events.append("start_listener")
            raise RuntimeError("listener failed")

    class FakeServer:
        def __init__(self, address, handler, *, runtime):
            events.append(f"server:{address[0]}:{address[1]}")

        def server_close(self):
            events.append("server_close")

    monkeypatch.setattr("my_wxauto.bridge_server.BridgeRuntime", FakeRuntime)
    monkeypatch.setattr("my_wxauto.bridge_server.BridgeHTTPServer", FakeServer)

    with pytest.raises(RuntimeError, match="listener failed"):
        create_bridge_server(BridgeServerConfig(port=0))

    assert events == [
        "runtime",
        "server:127.0.0.1:0",
        "start_listener",
        "server_close",
    ]


def test_run_bridge_server_closes_server(monkeypatch) -> None:
    events: list[str] = []

    class FakeRuntime:
        def __init__(self, config, **kwargs):
            events.append("runtime")

        def start_listener(self):
            events.append("start_listener")

    class FakeServer:
        def __init__(self, address, handler, *, runtime):
            events.append(f"server:{address[0]}:{address[1]}")

        def serve_forever(self):
            events.append("serve_forever")
            raise KeyboardInterrupt

        def server_close(self):
            events.append("server_close")

    monkeypatch.setattr("my_wxauto.bridge_server.BridgeRuntime", FakeRuntime)
    monkeypatch.setattr("my_wxauto.bridge_server.BridgeHTTPServer", FakeServer)

    with pytest.raises(KeyboardInterrupt):
        run_bridge_server(BridgeServerConfig(port=0))

    assert events == [
        "runtime",
        "server:127.0.0.1:0",
        "start_listener",
        "serve_forever",
        "server_close",
    ]


def test_runtime_enqueue_and_poll_events(tmp_path) -> None:
    runtime = BridgeRuntime(BridgeServerConfig(queue_size=2, store_path=tmp_path / "bridge.sqlite3"), wechat=FakeWeChat())

    runtime.enqueue_batch(_batch("one"))
    runtime.enqueue_batch(_batch("two"))

    payload = runtime.poll_events(timeout=0.0, limit=5)

    assert payload["status"] == "ok"
    assert payload["count"] == 2
    assert [event["messages"][0]["content"] for event in payload["events"]] == ["one", "two"]
    assert runtime.health()["queue_size"] == 0


def test_runtime_poll_events_times_out_empty(tmp_path) -> None:
    runtime = BridgeRuntime(BridgeServerConfig(queue_size=2, store_path=tmp_path / "bridge.sqlite3"), wechat=FakeWeChat())

    payload = runtime.poll_events(timeout=0.01, limit=5)

    assert payload == {"status": "ok", "count": 0, "events": []}


def test_runtime_poll_events_clamps_timeout_and_limit(tmp_path) -> None:
    runtime = BridgeRuntime(BridgeServerConfig(queue_size=60, store_path=tmp_path / "bridge.sqlite3"), wechat=FakeWeChat())
    for index in range(55):
        runtime.enqueue_batch(_batch(str(index)))

    payload = runtime.poll_events(timeout=-10, limit=1000)

    assert payload["status"] == "ok"
    assert payload["count"] == 50
    assert payload["events"][0]["messages"][0]["content"] == "0"
    assert payload["events"][-1]["messages"][0]["content"] == "49"
    assert runtime.health()["queue_size"] == 5


def test_runtime_enqueue_raises_when_queue_is_full(tmp_path) -> None:
    runtime = BridgeRuntime(BridgeServerConfig(queue_size=1, store_path=tmp_path / "bridge.sqlite3"), wechat=FakeWeChat())
    runtime.enqueue_batch(_batch("one"))

    with pytest.raises(queue.Full):
        runtime.enqueue_batch(_batch("two"))


def test_runtime_enqueue_persists_batch_until_ack_and_complete(tmp_path) -> None:
    store_path = tmp_path / "bridge.sqlite3"
    runtime = BridgeRuntime(BridgeServerConfig(store_path=store_path), wechat=FakeWeChat())

    runtime.enqueue_batch(_batch("one"))
    frozen_row = BridgeStore(store_path).get_batch("batch-one")
    payload = runtime.poll_events(timeout=0.0, limit=1)

    assert frozen_row is not None
    assert frozen_row["status"] == "frozen"
    assert payload["events"][0]["batch_id"] == "batch-one"
    assert BridgeStore(store_path).get_batch("batch-one")["status"] == "frozen"

    ack_payload = runtime.ack_event("batch-one")
    assert ack_payload == {"status": "ok", "batch_id": "batch-one", "batch_status": "submitted"}
    assert BridgeStore(store_path).get_batch("batch-one")["status"] == "submitted"

    complete_payload = runtime.complete_event("batch-one")
    assert complete_payload == {"status": "ok", "batch_id": "batch-one", "batch_status": "completed"}
    assert BridgeStore(store_path).get_batch("batch-one")["status"] == "completed"


def test_runtime_polls_persisted_pending_batches_after_restart(tmp_path) -> None:
    store_path = tmp_path / "bridge.sqlite3"
    store = BridgeStore(store_path)
    store.save_batch(_batch("persisted"))
    runtime = BridgeRuntime(BridgeServerConfig(store_path=store_path), wechat=FakeWeChat())

    payload = runtime.poll_events(timeout=0.0, limit=5)

    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["events"][0]["batch_id"] == "batch-persisted"


def test_http_ack_and_complete_event_endpoints(tmp_path) -> None:
    store_path = tmp_path / "bridge.sqlite3"
    runtime = BridgeRuntime(BridgeServerConfig(store_path=store_path), wechat=FakeWeChat())
    runtime.enqueue_batch(_batch("one"))

    with RunningServer(runtime) as server:
        ack_status, ack_content_type, ack_payload = server.request("POST", "/events/batch-one/ack")
        complete_status, complete_content_type, complete_payload = server.request("POST", "/events/batch-one/complete")

    assert ack_status == 200
    assert ack_content_type == "application/json; charset=utf-8"
    assert ack_payload == {"status": "ok", "batch_id": "batch-one", "batch_status": "submitted"}
    assert complete_status == 200
    assert complete_content_type == "application/json; charset=utf-8"
    assert complete_payload == {"status": "ok", "batch_id": "batch-one", "batch_status": "completed"}
    assert BridgeStore(store_path).get_batch("batch-one")["status"] == "completed"


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
    assert wx.listen_kwargs["mark_submitted_on_callback"] is False


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
