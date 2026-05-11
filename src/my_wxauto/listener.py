from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Callable

from . import probes
from .bridge_batcher import BatchingConfig, ConversationBatcher
from .bridge_events import ConversationBatch, messages_from_chat_payload
from .bridge_store import BridgeStore
from .window import WeChatWindowController, WindowRect


@dataclass(frozen=True)
class ChatMessage:
    content: str
    message_type: str = "unknown"
    sender: str | None = None
    is_self: bool | None = None
    time_text: str | None = None
    raw_name: str = ""
    class_name: str = ""
    automation_id: str = ""
    rect: dict[str, Any] | None = None
    visible_rect: dict[str, Any] | None = None

    @classmethod
    def from_probe(cls, payload: dict[str, Any]) -> "ChatMessage":
        content = str(payload.get("content") or payload.get("raw_name") or "")
        return cls(
            content=content,
            message_type=str(payload.get("message_type") or "unknown"),
            sender=_optional_str(payload.get("sender")),
            is_self=_optional_bool(payload.get("is_self")),
            time_text=_optional_str(payload.get("time_text")),
            raw_name=str(payload.get("raw_name") or content),
            class_name=str(payload.get("class_name") or ""),
            automation_id=str(payload.get("automation_id") or ""),
            rect=payload.get("rect") if isinstance(payload.get("rect"), dict) else None,
            visible_rect=payload.get("visible_rect") if isinstance(payload.get("visible_rect"), dict) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "message_type": self.message_type,
            "sender": self.sender,
            "is_self": self.is_self,
            "time_text": self.time_text,
            "raw_name": self.raw_name,
            "class_name": self.class_name,
            "automation_id": self.automation_id,
            "rect": self.rect or {},
            "visible_rect": self.visible_rect or {},
        }


@dataclass(frozen=True)
class NewMessageEvent:
    chat_name: str
    source: str
    messages: tuple[ChatMessage, ...]
    unread_count: int
    flash_index: int
    raw: dict[str, Any]

    @property
    def latest_message(self) -> ChatMessage | None:
        return self.messages[-1] if self.messages else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_name": self.chat_name,
            "source": self.source,
            "messages": [message.to_dict() for message in self.messages],
            "latest_message": self.latest_message.to_dict() if self.latest_message else None,
            "unread_count": self.unread_count,
            "flash_index": self.flash_index,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class ListenerStats:
    flash_count: int
    event_count: int
    duration_seconds: float
    stopped_reason: str


def listen_new_messages(
    callback: Callable[[NewMessageEvent], None],
    *,
    seconds: float = 0.0,
    interval: float = 0.25,
    max_controls: int = 260,
    min_changes: int = 4,
    window_seconds: float = 3.0,
    cooldown_seconds: float = 5.0,
    action_timeout: float = 15.0,
    max_events: int = 0,
    max_probes: int = 0,
) -> ListenerStats:
    """Listen for new WeChat messages and call callback with visible chat data.

    The first implementation uses taskbar/tray flashing as the wake-up signal.
    Passing seconds=0 listens until max_events/max_probes is reached or the
    caller interrupts the process.
    """

    probes._ensure_windows()
    detector = probes.TaskbarFlashDetector(
        min_changes=min_changes,
        window_seconds=window_seconds,
        cooldown_seconds=cooldown_seconds,
    )
    started = time.perf_counter()
    deadline = None if seconds <= 0 else time.monotonic() + seconds
    flash_count = 0
    event_count = 0
    stopped_reason = "timeout" if deadline is not None else "stopped"

    while deadline is None or time.monotonic() < deadline:
        icons = probes.inspect_wechat_taskbar_icons()
        flash = detector.observe(time.monotonic(), probes._taskbar_signature(icons))
        if flash is not None:
            flash_count += 1
            payload = probes._probe_sessions_after_wakeup_with_timeout(
                max_controls=max_controls,
                timeout=action_timeout,
                restore_icons=icons,
                open_unread_messages=True,
            )
            if str(payload.get("status") or "ok") == "ok":
                for event in _events_from_probe(payload, flash_index=flash_count):
                    callback(event)
                    event_count += 1
                    if max_events > 0 and event_count >= max_events:
                        stopped_reason = "max_events"
                        return _stats(started, flash_count, event_count, stopped_reason)
            if max_probes > 0 and flash_count >= max_probes:
                stopped_reason = "max_probes"
                break
        time.sleep(interval)

    return _stats(started, flash_count, event_count, stopped_reason)


def listen_conversation_batches(
    callback: Callable[[ConversationBatch], None],
    *,
    seconds: float = 0.0,
    interval: float = 0.25,
    max_controls: int = 260,
    min_changes: int = 4,
    window_seconds: float = 3.0,
    cooldown_seconds: float = 5.0,
    action_timeout: float = 15.0,
    max_events: int = 0,
    max_probes: int = 0,
    max_chats_per_drain: int = 5,
    max_ui_busy_seconds: float = 15.0,
    store_path: str | Path = ".my_wxauto_bridge.sqlite3",
    batching_config: BatchingConfig | None = None,
    resolve_senders: bool | str = False,
    sender_resolve_limit: int = 5,
    sender_resolve_timeout: float | None = 20.0,
    profile_card_timeout: float = 2.0,
    sender_progress: Callable[[dict[str, Any]], None] | None = None,
) -> ListenerStats:
    """Listen for unread chats and synchronously deliver frozen batches.

    The callback is a synchronous delivery hook. Production Hermes/model work
    should enqueue quickly inside the callback and return; this low-level
    listener intentionally does not own a model execution thread.

    With the default quiet window, max_probes may collect messages without
    emitting immediately. Callers that need immediate delivery can use a
    shorter quiet window or let a later loop iteration flush due batches.

    Sender resolution is disabled by default. Pass resolve_senders="profile_card"
    to click visible message avatars and read profile-card names before batching.
    This is slower and may briefly disturb the WeChat UI, so keep it opt-in.
    """

    probes._ensure_windows()
    detector = probes.TaskbarFlashDetector(
        min_changes=min_changes,
        window_seconds=window_seconds,
        cooldown_seconds=cooldown_seconds,
    )
    store = BridgeStore(store_path)
    batcher = ConversationBatcher(store, config=batching_config)
    started = time.perf_counter()
    deadline = None if seconds <= 0 else time.monotonic() + seconds
    flash_count = 0
    event_count = 0
    stopped_reason = "timeout" if deadline is not None else "stopped"
    stop_requested = False

    def emit_due(now: float) -> None:
        nonlocal event_count, stopped_reason, stop_requested
        remaining = None if max_events <= 0 else max_events - event_count
        if remaining is not None and remaining <= 0:
            return

        for batch in batcher.frozen_batches(limit=remaining):
            callback(batch)
            store.mark_batch_submitted(batch.batch_id, submitted_at=time.time())
            event_count += 1
            if max_events > 0 and event_count >= max_events:
                stopped_reason = "max_events"
                stop_requested = True
                return
        remaining = None if max_events <= 0 else max_events - event_count
        if remaining is not None and remaining <= 0:
            return
        for batch in batcher.freeze_due_batches(now=now, limit=remaining):
            callback(batch)
            store.mark_batch_submitted(batch.batch_id, submitted_at=time.time())
            event_count += 1
            if max_events > 0 and event_count >= max_events:
                stopped_reason = "max_events"
                stop_requested = True
                return

    def on_chat_opened(chat: dict[str, Any]) -> None:
        chat_name = str(chat.get("chat_name") or "")
        if not chat_name or not chat.get("messages"):
            return
        now = time.time()
        if _sender_resolution_enabled(resolve_senders):
            chat = _resolve_probe_chat_senders(
                chat,
                resolve_senders=resolve_senders,
                sender_resolve_limit=sender_resolve_limit,
                sender_resolve_timeout=sender_resolve_timeout,
                profile_card_timeout=profile_card_timeout,
                sender_progress=sender_progress,
            )
        messages = messages_from_chat_payload(chat)
        if not messages:
            return
        batcher.add_messages(chat_name, messages, now=now)
        emit_due(now)

    while deadline is None or time.monotonic() < deadline:
        icons = probes.inspect_wechat_taskbar_icons()
        flash = detector.observe(time.monotonic(), probes._taskbar_signature(icons))
        if flash is not None:
            flash_count += 1
            probes._probe_sessions_after_wakeup_with_timeout(
                max_controls=max_controls,
                timeout=action_timeout,
                restore_icons=icons,
                open_unread_messages=True,
                max_unread_chats=max_chats_per_drain,
                max_ui_busy_seconds=max_ui_busy_seconds,
                on_chat_opened=on_chat_opened,
            )
            if stop_requested:
                return _stats(started, flash_count, event_count, stopped_reason)
            if max_probes > 0 and flash_count >= max_probes:
                stopped_reason = "max_probes"
                break
        emit_due(time.time())
        if stop_requested:
            return _stats(started, flash_count, event_count, stopped_reason)
        time.sleep(interval)

    emit_due(time.time())
    return _stats(started, flash_count, event_count, stopped_reason)


def _resolve_probe_chat_senders(
    chat: dict[str, Any],
    *,
    resolve_senders: bool | str = False,
    sender_resolve_limit: int = 5,
    sender_resolve_timeout: float | None = 20.0,
    profile_card_timeout: float = 2.0,
    sender_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not _sender_resolution_enabled(resolve_senders):
        return chat

    region = _window_rect_from_dict(chat.get("message_region"))
    raw_messages = [
        message
        for message in (chat.get("messages") or [])
        if isinstance(message, dict)
    ]
    if not raw_messages:
        return chat

    if region is not None:
        raw_messages = [_message_with_visible_rect(message, region) for message in raw_messages]
        raw_messages = _annotate_messages_with_self_flags(raw_messages, region)

    messages = tuple(ChatMessage.from_probe(message) for message in raw_messages)
    if not messages:
        return chat

    resolved = _resolve_visible_message_senders(
        messages,
        lambda message: _resolve_sender_from_profile_card(
            message,
            timeout=profile_card_timeout,
            progress=sender_progress,
        ),
        limit=sender_resolve_limit,
        timeout=sender_resolve_timeout,
        progress=sender_progress,
        candidate_filter=_message_has_avatar_candidates,
    )
    return {**chat, "messages": [message.to_dict() for message in resolved]}


def _sender_resolution_enabled(resolve_senders: bool | str) -> bool:
    return resolve_senders is True or resolve_senders == "profile_card"


def _window_rect_from_dict(value: object) -> WindowRect | None:
    if not isinstance(value, dict):
        return None
    try:
        return WindowRect(
            left=int(value["left"]),
            top=int(value["top"]),
            right=int(value["right"]),
            bottom=int(value["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def get_latest_message(
    who: str,
    *,
    open_chat: Callable[[str], Any] | None = None,
    max_controls: int = 260,
) -> ChatMessage | None:
    messages = get_visible_messages(
        who,
        open_chat=open_chat,
        max_controls=max_controls,
        resolve_senders=False,
    )
    return messages[-1] if messages else None


def get_visible_messages(
    who: str,
    *,
    open_chat: Callable[[str], Any] | None = None,
    max_controls: int = 260,
    uia_collect_timeout: float = 5.0,
    resolve_senders: bool | str = False,
    sender_resolver: Callable[[ChatMessage], str | None] | None = None,
    sender_resolve_limit: int = 0,
    sender_resolve_timeout: float | None = 20.0,
    sender_progress: Callable[[dict[str, Any]], None] | None = None,
    profile_card_timeout: float = 2.0,
    reader_progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[ChatMessage, ...]:
    profile_card_mode = resolve_senders == "profile_card" or resolve_senders is True
    if open_chat is not None:
        _reader_progress(reader_progress, "open_chat.before", chat_name=who)
        open_chat(who)
        _reader_progress(reader_progress, "open_chat.after", chat_name=who)
    payload = _collect_current_chat_payload(
        chat_name=who,
        max_controls=max_controls,
        include_sessions=not profile_card_mode,
        uia_collect_timeout=uia_collect_timeout,
        progress=reader_progress,
    )
    events = _events_from_probe(payload, flash_index=0)
    if not events:
        return ()
    messages = events[0].messages
    if profile_card_mode:
        _reader_progress(reader_progress, "resolve_senders.before", count=len(messages))
        candidate_filter = None if sender_resolver is not None else _message_has_avatar_candidates
        resolver = sender_resolver or (
            lambda message: _resolve_sender_from_profile_card(
                message,
                timeout=profile_card_timeout,
                progress=sender_progress,
            )
        )
        messages = _resolve_visible_message_senders(
            messages,
            resolver,
            limit=sender_resolve_limit,
            timeout=sender_resolve_timeout,
            progress=sender_progress,
            candidate_filter=candidate_filter,
        )
        _reader_progress(reader_progress, "resolve_senders.after", count=len(messages))
    return messages


def _collect_current_chat_payload(
    *,
    chat_name: str,
    max_controls: int,
    include_sessions: bool = True,
    uia_collect_timeout: float = 5.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    controller = WeChatWindowController(prefer_tray_restore=False)
    _reader_progress(progress, "find_window.before")
    window = controller.find_main_window(reveal=True)
    _reader_progress(progress, "find_window.after", window=probes._window_to_dict(window))
    _reader_progress(progress, "activate.before", hwnd=window.hwnd)
    window = controller.activate(window)
    _reader_progress(progress, "activate.after", window=probes._window_to_dict(window))
    _reader_progress(progress, "wait_ready.before", hwnd=window.hwnd)
    ready = controller.wait_until_ready(window, timeout=5.0, stable_for=0.3)
    _reader_progress(progress, "wait_ready.after", window=probes._window_to_dict(ready))

    sessions: list[dict[str, Any]] = []
    if include_sessions:
        sessions = _collect_sessions_from_window(
            ready,
            max_controls=max_controls,
            uia_collect_timeout=uia_collect_timeout,
            progress=progress,
        )

    message_region, message_controls = _collect_message_controls_from_window(
        ready,
        max_controls=max_controls,
        uia_collect_timeout=uia_collect_timeout,
        progress=progress,
    )
    if not message_controls:
        recovered = _recover_blank_wechat_window(controller, ready, progress=progress)
        if recovered is not None:
            ready = recovered
            if include_sessions:
                sessions = _collect_sessions_from_window(
                    ready,
                    max_controls=max_controls,
                    uia_collect_timeout=uia_collect_timeout,
                    progress=progress,
                )
            message_region, message_controls = _collect_message_controls_from_window(
                ready,
                max_controls=max_controls,
                uia_collect_timeout=uia_collect_timeout,
                progress=progress,
            )
    messages = [
        _message_with_visible_rect(message, message_region)
        for message in probes._parse_chat_message_items(message_controls)
    ]
    messages = _annotate_messages_with_self_flags(messages, message_region)
    _reader_progress(
        progress,
        "collect_messages.after",
        controls=len(message_controls),
        messages=len(messages),
    )
    return {
        "window": probes._window_to_dict(ready),
        "sessions": sessions,
        "unread_count": 0,
        "opened_unread_chats": [
            {
                "chat_name": chat_name,
                "source": "current_chat",
                "status": "ok",
                "message_region": probes._rect_to_dict(message_region),
                "uia": {
                    "count": len(message_controls),
                    "controls": message_controls,
                },
                "messages": messages,
            }
        ],
    }


def _collect_sessions_from_window(
    window: Any,
    *,
    max_controls: int,
    uia_collect_timeout: float,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    session_region = probes._session_list_region(window)
    _reader_progress(progress, "collect_sessions.before", region=probes._rect_to_dict(session_region))
    session_controls = _collect_region_controls_with_retries(
        window,
        region=session_region,
        max_controls=max_controls,
        timeout=uia_collect_timeout,
        progress=progress,
        label="collect_sessions",
    )
    sessions = probes._parse_session_items(session_controls)
    _reader_progress(
        progress,
        "collect_sessions.after",
        controls=len(session_controls),
        sessions=len(sessions),
    )
    return sessions


def _collect_message_controls_from_window(
    window: Any,
    *,
    max_controls: int,
    uia_collect_timeout: float,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    message_region = probes._chat_message_region(window)
    _reader_progress(progress, "collect_messages.before", region=probes._rect_to_dict(message_region))
    message_controls = _collect_region_controls_with_retries(
        window,
        region=message_region,
        max_controls=max_controls,
        timeout=uia_collect_timeout,
        progress=progress,
        label="collect_messages",
    )
    _reader_progress(progress, "collect_messages.after", controls=len(message_controls))
    return message_region, message_controls


def _recover_blank_wechat_window(
    controller: Any,
    window: Any,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Any | None:
    _reader_progress(progress, "blank_recover.before", hwnd=getattr(window, "hwnd", None))
    current = window
    for attempt in range(1, 3):
        _reader_progress(progress, "blank_recover.hide.before", attempt=attempt)
        probes._send_wechat_toggle_hotkey()
        time.sleep(0.8)
        current = controller.get_window(window.hwnd) or window
        _reader_progress(progress, "blank_recover.hide.after", attempt=attempt, window=probes._window_to_dict(current))
        if not current.visible:
            break
    _reader_progress(progress, "blank_recover.show.before")
    probes._send_wechat_toggle_hotkey()
    time.sleep(1.2)
    _reader_progress(progress, "blank_recover.show.after")
    visible_window = probes._wait_for_visible_window_after_reveal(controller, window, timeout=3.0)
    if visible_window is None:
        try:
            visible_window = controller.find_main_window(reveal=True)
        except Exception:
            _reader_progress(progress, "blank_recover.failed")
            return None
    activated = controller.activate(visible_window, wait=0.5)
    ready = controller.wait_until_ready(activated, timeout=5.0, stable_for=0.3, min_wait=0.5)
    _reader_progress(progress, "blank_recover.after", window=probes._window_to_dict(ready))
    return ready


def _collect_region_controls_with_retries(
    window: Any,
    *,
    region: Any,
    max_controls: int,
    timeout: float = 5.0,
    attempts: int = 4,
    delay: float = 0.4,
    progress: Callable[[dict[str, Any]], None] | None = None,
    label: str = "collect_controls",
) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    for attempt in range(1, max(1, attempts) + 1):
        controls = _collect_uia_controls_with_timeout(
            window,
            region=region,
            max_controls=max_controls,
            timeout=timeout,
        )
        if controls or attempt >= attempts:
            return controls
        _reader_progress(progress, f"{label}.retry", attempt=attempt, delay=delay)
        time.sleep(delay)
    return controls


def _collect_uia_controls_with_timeout(
    window: Any,
    *,
    region: Any,
    max_controls: int,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    from pywinauto import Desktop

    deadline = None if timeout <= 0 else time.monotonic() + timeout
    desktop = Desktop(backend="uia")
    wrapper = desktop.window(handle=window.hwnd)
    controls: list[dict[str, Any]] = []
    try:
        candidates = wrapper.descendants(control_type="ListItem")
    except Exception:
        candidates = []

    for control in candidates:
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            rect = control.rectangle()
        except Exception:
            continue
        control_rect = type(region)(rect.left, rect.top, rect.right, rect.bottom)
        if not probes._rects_intersect(control_rect, region):
            continue
        element_info = getattr(control, "element_info", None)
        name = str(getattr(element_info, "name", "") or "")
        control_type = str(getattr(element_info, "control_type", "") or "")
        class_name = str(getattr(element_info, "class_name", "") or "")
        automation_id = str(getattr(element_info, "automation_id", "") or "")
        if not name and not control_type and not class_name and not automation_id:
            continue
        controls.append(
            {
                "name": name,
                "control_type": control_type,
                "class_name": class_name,
                "automation_id": automation_id,
                "rect": probes._rect_to_dict(control_rect),
            }
        )
        if len(controls) >= max_controls:
            break
    return controls


def _message_with_visible_rect(message: dict[str, Any], region: Any) -> dict[str, Any]:
    rect = message.get("rect")
    if not isinstance(rect, dict):
        return message
    try:
        left = max(int(rect["left"]), int(region.left))
        top = max(int(rect["top"]), int(region.top))
        right = min(int(rect["right"]), int(region.right))
        bottom = min(int(rect["bottom"]), int(region.bottom))
    except (KeyError, TypeError, ValueError):
        return message
    if right <= left or bottom <= top:
        visible_rect = {"left": 0, "top": 0, "right": 0, "bottom": 0, "width": 0, "height": 0}
    else:
        visible_rect = {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "width": right - left,
            "height": bottom - top,
        }
    return {**message, "visible_rect": visible_rect}


def _annotate_messages_with_self_flags(messages: list[dict[str, Any]], region: Any) -> list[dict[str, Any]]:
    try:
        width = int(region.width)
        height = int(region.height)
        if width <= 0 or height <= 0:
            return messages
        screen_bgra = probes._capture_screen_bgra(region.left, region.top, width, height)
    except Exception:
        return messages

    annotated: list[dict[str, Any]] = []
    for message in messages:
        if message.get("is_self") is not None:
            annotated.append(message)
            continue
        is_self = _infer_message_is_self_from_pixels(message, region, screen_bgra)
        annotated.append({**message, "is_self": is_self})
    return annotated


def _infer_message_is_self_from_pixels(message: dict[str, Any], region: Any, screen_bgra: bytes) -> bool | None:
    if str(message.get("message_type") or "") != "text":
        return None
    rect = message.get("visible_rect") or message.get("rect")
    if not isinstance(rect, dict):
        return None
    try:
        left = max(int(rect["left"]), int(region.left)) - int(region.left)
        top = max(int(rect["top"]), int(region.top)) - int(region.top)
        right = min(int(rect["right"]), int(region.right)) - int(region.left)
        bottom = min(int(rect["bottom"]), int(region.bottom)) - int(region.top)
        width = int(region.width)
    except (KeyError, TypeError, ValueError):
        return None
    if right <= left or bottom <= top or width <= 0:
        return None

    midpoint = left + (right - left) // 2
    right_green = 0
    left_green = 0
    total = 0
    for y in range(max(0, top), max(0, bottom), 2):
        for x in range(max(0, left), max(0, right), 2):
            offset = (y * width + x) * 4
            if offset + 3 >= len(screen_bgra):
                continue
            blue = screen_bgra[offset]
            green = screen_bgra[offset + 1]
            red = screen_bgra[offset + 2]
            total += 1
            if _looks_like_self_bubble_pixel(red=red, green=green, blue=blue):
                if x >= midpoint:
                    right_green += 1
                else:
                    left_green += 1

    if total <= 0:
        return None
    green_ratio = right_green / total
    if right_green >= 80 and green_ratio >= 0.008 and right_green >= max(20, left_green * 2):
        return True
    return False


def _looks_like_self_bubble_pixel(*, red: int, green: int, blue: int) -> bool:
    return (
        green >= 175
        and 70 <= red <= 190
        and blue <= 170
        and green - red >= 30
        and green - blue >= 40
    )


def _events_from_probe(payload: dict[str, Any], *, flash_index: int) -> list[NewMessageEvent]:
    unread_count = _safe_int(payload.get("unread_count"))
    events: list[NewMessageEvent] = []
    for chat in payload.get("opened_unread_chats") or []:
        if not isinstance(chat, dict):
            continue
        chat_name = str(chat.get("chat_name") or "")
        messages = tuple(
            ChatMessage.from_probe(message)
            for message in chat.get("messages") or []
            if isinstance(message, dict)
        )
        if not messages:
            continue
        inferred_latest_sender = _latest_sender_from_session_preview(
            payload,
            chat_name=chat_name,
            latest_content=messages[-1].content,
        )
        if inferred_latest_sender and messages[-1].sender is None:
            messages = (
                *messages[:-1],
                ChatMessage(
                    content=messages[-1].content,
                    message_type=messages[-1].message_type,
                    sender=inferred_latest_sender,
                    is_self=messages[-1].is_self,
                    time_text=messages[-1].time_text,
                    raw_name=messages[-1].raw_name,
                    class_name=messages[-1].class_name,
                    automation_id=messages[-1].automation_id,
                    rect=messages[-1].rect,
                    visible_rect=messages[-1].visible_rect,
                ),
            )
        events.append(
            NewMessageEvent(
                chat_name=chat_name,
                source=str(chat.get("source") or ""),
                messages=messages,
                unread_count=unread_count,
                flash_index=flash_index,
                raw=chat,
            )
        )
    return events


def _reader_progress(
    progress: Callable[[dict[str, Any]], None] | None,
    stage: str,
    **extra: object,
) -> None:
    if progress is None:
        return
    progress({"stage": stage, **extra})


def _resolve_visible_message_senders(
    messages: tuple[ChatMessage, ...],
    resolver: Callable[[ChatMessage], str | None],
    *,
    limit: int = 0,
    timeout: float | None = 20.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
    candidate_filter: Callable[[ChatMessage], bool] | None = None,
) -> tuple[ChatMessage, ...]:
    deadline = None if timeout is None or timeout <= 0 else time.monotonic() + timeout
    resolved: list[ChatMessage] = []
    attempts = 0
    total = len(messages)
    for index, message in enumerate(messages, 1):
        if message.sender:
            resolved.append(message)
            continue
        if message.is_self is True:
            _sender_progress(progress, "skipped_self", message, index=index, total=total, attempts=attempts)
            resolved.append(message)
            continue
        if candidate_filter is not None and not candidate_filter(message):
            _sender_progress(progress, "skipped_no_avatar", message, index=index, total=total, attempts=attempts)
            resolved.append(message)
            continue
        if limit > 0 and attempts >= limit:
            _sender_progress(progress, "skipped_limit", message, index=index, total=total, attempts=attempts)
            resolved.append(message)
            continue
        if deadline is not None and time.monotonic() >= deadline:
            _sender_progress(progress, "skipped_timeout", message, index=index, total=total, attempts=attempts)
            resolved.append(message)
            continue
        attempts += 1
        _sender_progress(progress, "start", message, index=index, total=total, attempts=attempts)
        sender = resolver(message)
        if sender:
            _sender_progress(
                progress,
                "resolved",
                message,
                index=index,
                total=total,
                attempts=attempts,
                sender=sender,
            )
            resolved.append(_copy_message(message, sender=sender))
        else:
            _sender_progress(progress, "unresolved", message, index=index, total=total, attempts=attempts)
            resolved.append(message)
    return tuple(resolved)


def _sender_progress(
    progress: Callable[[dict[str, Any]], None] | None,
    stage: str,
    message: ChatMessage,
    **extra: object,
) -> None:
    if progress is None:
        return
    event = {
        "stage": stage,
        "content": message.content,
        "sender": message.sender,
        "message_type": message.message_type,
        **extra,
    }
    progress(event)


def _resolve_sender_from_profile_card(
    message: ChatMessage,
    *,
    timeout: float = 2.0,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> str | None:
    point_candidates = _avatar_click_points(message)
    if not point_candidates:
        return None

    from .keyboard import KeyboardController

    keyboard = KeyboardController()
    _sender_progress(progress, "profile_points", message, points=point_candidates)
    for point in point_candidates:
        started = time.monotonic()
        try:
            _sender_progress(progress, "profile_click.before", message, point=point)
            probes._click_point(point)
            time.sleep(0.45)
            handles = _profile_card_window_handles()
            _sender_progress(progress, "profile_click.after", message, point=point, handles=handles)
            if not handles:
                continue
            elapsed = time.monotonic() - started
            remaining = max(0.2, timeout - elapsed) if timeout > 0 else timeout
            sender = _read_profile_card_name_with_timeout(timeout=remaining, handles=handles)
            keyboard.press("esc")
            time.sleep(0.15)
            if sender:
                _sender_progress(progress, "profile_name", message, point=point, sender=sender)
                return sender
        except Exception:
            try:
                keyboard.press("esc")
            except Exception:
                pass
    return None


def _avatar_click_points(message: ChatMessage) -> list[tuple[int, int]]:
    rect = message.visible_rect or message.rect or {}
    try:
        left = int(rect["left"])
        top = int(rect["top"])
        right = int(rect["right"])
        bottom = int(rect["bottom"])
    except (KeyError, TypeError, ValueError):
        return []
    if right <= left or bottom <= top:
        return []
    if bottom - top < 24 or right - left < 120:
        return []

    y = (top + bottom) // 2
    return [
        (left + 8, y),
        (left - 2, y),
        (left + 18, y),
        (left + 28, y),
        (right - 80, y),
        (right - 92, y),
        (right - 68, y),
        (right - 104, y),
    ]


def _message_has_avatar_candidates(message: ChatMessage) -> bool:
    if message.message_type != "text":
        return False
    return bool(_avatar_click_points(message))


def _read_profile_card_name(
    *,
    timeout: float = 2.0,
    max_controls: int = 80,
    handles: list[int] | None = None,
) -> str | None:
    from pywinauto import Desktop

    handles = handles if handles is not None else _profile_card_window_handles()
    if not handles:
        return None
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    desktop = Desktop(backend="uia")
    candidates: list[tuple[int, int, int, str]] = []
    for window in _profile_card_windows(desktop, handles=handles):
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            for control in _iter_limited_uia_tree(window, deadline=deadline, max_controls=max_controls):
                cinfo = control.element_info
                name = str(getattr(cinfo, "name", "") or "").strip()
                control_type = str(getattr(cinfo, "control_type", "") or "")
                cclass = str(getattr(cinfo, "class_name", "") or "")
                if not name or control_type not in {"Text", "Edit", "Button"}:
                    continue
                automation_id = str(getattr(cinfo, "automation_id", "") or "")
                priority = _profile_name_priority(
                    name,
                    control_type=control_type,
                    class_name=cclass,
                    automation_id=automation_id,
                )
                if priority is None:
                    continue
                try:
                    rect = control.rectangle()
                    top = int(rect.top)
                except Exception:
                    top = 0
                candidates.append((priority, top, len(name), name))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]


def _read_profile_card_name_with_timeout(
    *,
    timeout: float = 2.0,
    max_controls: int = 80,
    handles: list[int] | None = None,
) -> str | None:
    kwargs: dict[str, Any] = {"timeout": timeout, "max_controls": max_controls}
    if handles is not None:
        kwargs["handles"] = handles
    if timeout <= 0:
        return _read_profile_card_name(**kwargs)

    result_queue: Queue[str | None] = Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put(_read_profile_card_name(**kwargs))
        except Exception:
            result_queue.put(None)

    thread = Thread(target=worker, name="my-wxauto-profile-card-reader", daemon=True)
    thread.start()
    try:
        return result_queue.get(timeout=timeout)
    except Empty:
        return None


def _profile_card_windows(desktop: Any, *, handles: list[int] | None = None) -> list[Any]:
    hwnds = handles if handles is not None else _profile_card_window_handles()
    windows: list[Any] = []
    for hwnd in hwnds:
        try:
            windows.append(desktop.window(handle=hwnd))
        except Exception:
            continue
    return windows


def _profile_card_window_handles() -> list[int]:
    try:
        import psutil
        import win32gui
        import win32process
    except Exception:
        return []

    handles: list[int] = []

    def maybe_add(hwnd: int) -> None:
        if not hwnd or hwnd in handles or not win32gui.IsWindowVisible(hwnd):
            return
        class_name = win32gui.GetClassName(hwnd) or ""
        title = win32gui.GetWindowText(hwnd) or ""
        if not _looks_like_profile_card_window(class_name=class_name, title=title):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            process_name = (psutil.Process(pid).name() or "").lower()
        except Exception:
            process_name = ""
        if process_name not in {"wechat.exe", "weixin.exe"}:
            return
        handles.append(hwnd)

    try:
        maybe_add(win32gui.GetForegroundWindow())

        def callback(hwnd: int, _extra: object) -> bool:
            maybe_add(hwnd)
            return True

        win32gui.EnumWindows(callback, None)
    except Exception:
        return handles
    return handles


def _looks_like_profile_card_window(*, class_name: str, title: str) -> bool:
    if "ToolSaveBits" in class_name or "Tool" in class_name or "Profile" in class_name:
        return True
    return title == "Weixin"


def _iter_limited_uia_tree(root: Any, *, deadline: float | None, max_controls: int) -> list[Any]:
    controls: list[Any] = []
    stack = [root]
    seen: set[int] = set()
    while stack and len(controls) < max_controls:
        if deadline is not None and time.monotonic() >= deadline:
            break
        control = stack.pop()
        identity = id(control)
        if identity in seen:
            continue
        seen.add(identity)
        controls.append(control)
        try:
            children = list(control.children())
        except Exception:
            children = []
        stack.extend(reversed(children))
    return controls


def _looks_like_profile_name(value: str) -> bool:
    text = value.strip()
    if not text or len(text) > 40:
        return False
    blocked = {
        "微信",
        "消息",
        "发送",
        "聊天信息",
        "朋友圈",
        "视频号",
        "设置备注和标签",
        "发消息",
        "音视频通话",
        "备注",
        "昵称：",
        "微信号：",
        "地区：",
        "朋友资料",
        "更多",
    }
    if text in blocked:
        return False
    return "\n" not in text


def _profile_name_priority(
    value: str,
    *,
    control_type: str,
    class_name: str,
    automation_id: str,
) -> int | None:
    if not _looks_like_profile_name(value):
        return None
    if automation_id.endswith("display_name_text"):
        return 0
    if "ContactHeadView" in class_name and control_type == "Button":
        return 1
    if "ProfileDetailValueRemarkView" in class_name:
        return 2
    if "ProfileTextView" in class_name:
        return 3
    return None


def _stats(started: float, flash_count: int, event_count: int, stopped_reason: str) -> ListenerStats:
    return ListenerStats(
        flash_count=flash_count,
        event_count=event_count,
        duration_seconds=round(time.perf_counter() - started, 3),
        stopped_reason=stopped_reason,
    )


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _latest_sender_from_session_preview(
    payload: dict[str, Any],
    *,
    chat_name: str,
    latest_content: str,
) -> str | None:
    for session in payload.get("sessions") or []:
        if not isinstance(session, dict) or str(session.get("chat_name") or "") != chat_name:
            continue
        preview = str(session.get("preview") or "").strip()
        for separator in (": ", "：", ":"):
            if separator not in preview:
                continue
            sender, content = preview.split(separator, 1)
            sender = sender.strip()
            content = content.strip()
            if sender and content and _message_preview_matches(content, latest_content):
                return sender
    return None


def _message_preview_matches(preview_content: str, latest_content: str) -> bool:
    preview = preview_content.strip()
    latest = latest_content.strip()
    if not preview or not latest:
        return False
    return latest == preview or latest.startswith(preview) or preview.startswith(latest)


def _copy_message(message: ChatMessage, *, sender: str | None = None) -> ChatMessage:
    return ChatMessage(
        content=message.content,
        message_type=message.message_type,
        sender=sender if sender is not None else message.sender,
        is_self=message.is_self,
        time_text=message.time_text,
        raw_name=message.raw_name,
        class_name=message.class_name,
        automation_id=message.automation_id,
        rect=message.rect,
        visible_rect=message.visible_rect,
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None
