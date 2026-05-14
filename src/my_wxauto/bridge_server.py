from __future__ import annotations

import json
import queue
import threading as _threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .bridge_events import ConversationBatch
from .bridge_store import BridgeStore
from .wechat import WeChat


MAX_JSON_BODY_BYTES = 1024 * 1024


class _ThreadingProxy:
    Lock = staticmethod(_threading.Lock)
    RLock = staticmethod(_threading.RLock)
    Thread = _threading.Thread


threading = _ThreadingProxy()


@dataclass(frozen=True)
class BridgeServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    store_path: str | Path = ".my_wxauto_bridge.sqlite3"
    queue_size: int = 100
    listen_interval: float = 0.25
    max_chats_per_drain: int = 5
    resolve_senders: bool | str = False
    sender_resolve_limit: int = 5
    prefer_wxauto4: bool = True
    debug: bool = False
    trace_ui: bool = False


class BridgeRuntime:
    def __init__(
        self,
        config: BridgeServerConfig,
        *,
        wechat: Any | None = None,
        ui_lock: Any | None = None,
    ) -> None:
        self.config = config
        self.ui_lock = ui_lock or threading.RLock()
        self.wechat = wechat or WeChat(
            prefer_wxauto4=config.prefer_wxauto4,
            debug=config.debug,
            trace_ui=config.trace_ui,
            bridge_store_path=config.store_path,
        )
        self.store = BridgeStore(config.store_path)
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=config.queue_size)
        self._listener_thread: threading.Thread | None = None
        self._listener_lock = threading.Lock()

    def health(self) -> dict[str, Any]:
        thread = self._listener_thread
        return {
            "status": "ok",
            "queue_size": self._events.qsize(),
            "listener_alive": bool(thread and thread.is_alive()),
            "store_path": str(self.config.store_path),
        }

    def enqueue_batch(self, batch: ConversationBatch) -> None:
        self.store.save_batch(batch)
        self._events.put_nowait(batch.to_event_dict())

    def poll_events(self, *, timeout: float = 30.0, limit: int = 5) -> dict[str, Any]:
        timeout = _clamp_float(timeout, minimum=0.0, maximum=120.0, default=30.0)
        limit = _clamp_int(limit, minimum=1, maximum=50, default=5)
        events: list[dict[str, Any]] = []
        seen_batch_ids: set[str] = set()

        def add_event(event: dict[str, Any]) -> None:
            batch_id = str(event.get("batch_id") or event.get("event_id") or "")
            if batch_id and batch_id in seen_batch_ids:
                return
            if batch_id:
                seen_batch_ids.add(batch_id)
            events.append(event)

        while len(events) < limit:
            try:
                add_event(self._events.get_nowait())
            except queue.Empty:
                break

        self._append_persisted_pending_events(events, seen_batch_ids, limit=limit)
        if events:
            return {"status": "ok", "count": len(events), "events": events}

        try:
            add_event(self._events.get(timeout=timeout))
        except queue.Empty:
            return {"status": "ok", "count": 0, "events": []}

        while len(events) < limit:
            try:
                add_event(self._events.get_nowait())
            except queue.Empty:
                break
        self._append_persisted_pending_events(events, seen_batch_ids, limit=limit)
        return {"status": "ok", "count": len(events), "events": events}

    def ack_event(self, batch_id: str) -> dict[str, Any]:
        self.store.mark_batch_submitted(batch_id)
        return {"status": "ok", "batch_id": batch_id, "batch_status": "submitted"}

    def complete_event(self, batch_id: str) -> dict[str, Any]:
        self.store.mark_batch_completed(batch_id)
        return {"status": "ok", "batch_id": batch_id, "batch_status": "completed"}

    def send_message(self, who: str, message: str) -> dict[str, Any]:
        with self.ui_lock:
            response = self.wechat.SendMsg(message, who)
        return response.to_dict() if hasattr(response, "to_dict") else dict(response)

    def start_listener(self) -> None:
        with self._listener_lock:
            if self._listener_thread is not None and self._listener_thread.is_alive():
                return
            self._listener_thread = threading.Thread(target=self._listener_target, daemon=True)
            self._listener_thread.start()

    def _listener_target(self) -> None:
        self.wechat.listen_conversation_batches(
            self.enqueue_batch,
            interval=self.config.listen_interval,
            max_chats_per_drain=self.config.max_chats_per_drain,
            store_path=self.config.store_path,
            resolve_senders=self.config.resolve_senders,
            sender_resolve_limit=self.config.sender_resolve_limit,
            ui_lock=self.ui_lock,
            mark_submitted_on_callback=False,
        )

    def _append_persisted_pending_events(
        self,
        events: list[dict[str, Any]],
        seen_batch_ids: set[str],
        *,
        limit: int,
    ) -> None:
        if len(events) >= limit:
            return
        for status in ("frozen", "submitted"):
            for row in self.store.list_batches(status=status):
                if len(events) >= limit:
                    return
                batch_id = str(row["batch_id"])
                if batch_id in seen_batch_ids:
                    continue
                payload = json.loads(row["payload_json"])
                seen_batch_ids.add(batch_id)
                events.append(payload)


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        *,
        runtime: BridgeRuntime,
        bind_and_activate: bool = True,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass, bind_and_activate=bind_and_activate)
        self.runtime = runtime


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeHTTPServer

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        if parsed_url.path == "/health":
            self._send_json(200, self.server.runtime.health())
            return

        if parsed_url.path == "/events":
            query = parse_qs(parsed_url.query)
            timeout = _first_query_value(query, "timeout", 30.0)
            limit = _first_query_value(query, "limit", 5)
            payload = self.server.runtime.poll_events(timeout=timeout, limit=limit)
            self._send_json(200, payload)
            return

        self._send_json(404, {"status": "error", "message": "not found"})

    def do_POST(self) -> None:
        parsed_url = urlparse(self.path)
        event_action = _event_action_from_path(parsed_url.path)
        if event_action is not None:
            batch_id, action = event_action
            try:
                if action == "ack":
                    self._send_json(200, self.server.runtime.ack_event(batch_id))
                else:
                    self._send_json(200, self.server.runtime.complete_event(batch_id))
            except KeyError:
                self._send_json(404, {"status": "error", "message": f"batch not found: {batch_id}"})
            return

        if parsed_url.path != "/send":
            self._send_json(404, {"status": "error", "message": "not found"})
            return

        payload = self._read_json_body()
        if payload is None:
            return

        who = payload.get("who")
        message = payload.get("message")
        if not isinstance(who, str) or not who or not isinstance(message, str) or not message:
            self._send_json(400, {"status": "error", "message": "who and message are required"})
            return

        self._send_json(200, self.server.runtime.send_message(who, message))

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_json_body(self) -> dict[str, Any] | None:
        content_length_header = self.headers.get("Content-Length", "0")
        try:
            content_length = int(content_length_header)
        except ValueError:
            self._send_json(400, {"status": "error", "message": "Invalid Content-Length"})
            return None
        if content_length < 0:
            self._send_json(400, {"status": "error", "message": "Invalid Content-Length"})
            return None
        if content_length > MAX_JSON_BODY_BYTES:
            self._send_json(413, {"status": "error", "message": "JSON body too large"})
            return None
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"status": "error", "message": "Invalid JSON body"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"status": "error", "message": "JSON body must be an object"})
            return None
        return payload

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        response_body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass


def create_bridge_server(
    config: BridgeServerConfig,
    *,
    wechat: Any | None = None,
    ui_lock: Any | None = None,
) -> BridgeHTTPServer:
    runtime = BridgeRuntime(config, wechat=wechat, ui_lock=ui_lock)
    server = BridgeHTTPServer((config.host, config.port), BridgeRequestHandler, runtime=runtime)
    try:
        runtime.start_listener()
    except Exception:
        server.server_close()
        raise
    return server


def run_bridge_server(config: BridgeServerConfig) -> None:
    server = create_bridge_server(config)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _clamp_float(value: object, *, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _first_query_value(query: dict[str, list[str]], key: str, default: object) -> object:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def _clamp_int(value: object, *, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _event_action_from_path(path: str) -> tuple[str, str] | None:
    parts = [unquote(part) for part in path.split("/") if part]
    if len(parts) != 3 or parts[0] != "events" or parts[2] not in {"ack", "complete"}:
        return None
    return parts[1], parts[2]
