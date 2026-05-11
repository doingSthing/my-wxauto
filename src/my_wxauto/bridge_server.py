from __future__ import annotations

import queue
import threading as _threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bridge_events import ConversationBatch
from .wechat import WeChat


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
        self._events.put_nowait(batch.to_event_dict())

    def poll_events(self, *, timeout: float = 30.0, limit: int = 5) -> dict[str, Any]:
        timeout = _clamp_float(timeout, minimum=0.0, maximum=120.0, default=30.0)
        limit = _clamp_int(limit, minimum=1, maximum=50, default=5)
        events: list[dict[str, Any]] = []
        try:
            events.append(self._events.get(timeout=timeout))
        except queue.Empty:
            return {"status": "ok", "count": 0, "events": []}

        while len(events) < limit:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return {"status": "ok", "count": len(events), "events": events}

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
        )


def _clamp_float(value: object, *, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _clamp_int(value: object, *, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
