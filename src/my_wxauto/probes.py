from __future__ import annotations

import ctypes
import hashlib
import json
import multiprocessing as mp
import platform
import queue
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from .keyboard import KeyboardController
from .window import WeChatWindow, WeChatWindowController, WindowRect


@dataclass(frozen=True)
class RedBadgeCandidate:
    rect: dict[str, int]
    center: tuple[int, int]
    area: int


@dataclass
class TaskbarFlashDetector:
    min_changes: int = 4
    window_seconds: float = 3.0
    cooldown_seconds: float = 5.0
    _last_signature: tuple[Any, ...] | None = None
    _change_times: list[float] = field(default_factory=list)
    _cooldown_until: float = 0.0

    def observe(self, now: float, signature: tuple[Any, ...]) -> dict[str, Any] | None:
        if self._last_signature is None:
            self._last_signature = signature
            return None
        if signature == self._last_signature:
            return None

        self._last_signature = signature
        threshold = now - self.window_seconds
        self._change_times = [changed_at for changed_at in self._change_times if changed_at >= threshold]
        self._change_times.append(now)
        if now < self._cooldown_until or len(self._change_times) < self.min_changes:
            return None

        first_change = self._change_times[0]
        change_count = len(self._change_times)
        self._change_times = []
        self._cooldown_until = now + self.cooldown_seconds
        return {
            "change_count": change_count,
            "window_seconds": self.window_seconds,
            "duration_seconds": round(now - first_change, 3),
            "cooldown_seconds": self.cooldown_seconds,
        }


def probe_listener_signals(
    *,
    include_uia: bool = True,
    include_badges: bool = True,
    include_taskbar: bool = False,
    max_controls: int = 160,
) -> dict[str, Any]:
    """Return a diagnostic snapshot for listener signal discovery.

    This is intentionally read-only. It samples the active WeChat window,
    visible UIA controls in the left session-list area, and red badge-like
    regions in the same area.
    """

    _ensure_windows()
    started = time.perf_counter()
    controller = WeChatWindowController()
    window = controller.find_main_window(reveal=False)
    region = _session_list_region(window)
    payload: dict[str, Any] = {
        "window": _window_to_dict(window),
        "session_region": _rect_to_dict(region),
    }
    if include_uia:
        uia_started = time.perf_counter()
        controls = _collect_uia_controls(window, region=region, max_controls=max_controls)
        payload["uia"] = {
            "duration_ms": round((time.perf_counter() - uia_started) * 1000, 1),
            "count": len(controls),
            "controls": controls,
        }
        payload["sessions"] = _parse_session_items(controls)
    if include_badges:
        badge_started = time.perf_counter()
        badges = find_red_badges(region)
        payload["red_badges"] = {
            "duration_ms": round((time.perf_counter() - badge_started) * 1000, 1),
            "count": len(badges),
            "candidates": [asdict(badge) for badge in badges],
        }
    if include_taskbar:
        taskbar_started = time.perf_counter()
        taskbar_icons = inspect_wechat_taskbar_icons()
        payload["taskbar"] = {
            "duration_ms": round((time.perf_counter() - taskbar_started) * 1000, 1),
            "count": len(taskbar_icons),
            "icons": taskbar_icons,
        }
    payload["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return payload


def watch_listener_signals(
    *,
    seconds: float,
    interval: float = 0.5,
    include_uia: bool = False,
    include_badges: bool = True,
    include_taskbar: bool = False,
    max_controls: int = 80,
) -> None:
    """Print signal snapshots when the visible listener signals change."""

    deadline = time.monotonic() + seconds
    last_signature: tuple[Any, ...] | None = None
    while time.monotonic() < deadline:
        try:
            snapshot = probe_listener_signals(
                include_uia=include_uia,
                include_badges=include_badges,
                include_taskbar=include_taskbar,
                max_controls=max_controls,
            )
            signature = _snapshot_signature(snapshot)
            if signature != last_signature:
                last_signature = signature
                print("MY_WXAUTO_SIGNAL " + json.dumps(snapshot, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(
                "MY_WXAUTO_SIGNAL "
                + json.dumps({"status": "error", "error": repr(exc)}, ensure_ascii=False),
                flush=True,
            )
        time.sleep(interval)


def watch_taskbar_icons(*, seconds: float, interval: float = 0.25) -> None:
    """Print taskbar/tray screenshots when their pixels change.

    This deliberately avoids UIAutomation because taskbar UIA enumeration can
    block on some Windows builds. It is only a wake-up signal probe: it tells us
    whether the taskbar/tray pixels changed while WeChat is flashing, not which
    exact control changed.
    """

    deadline = time.monotonic() + seconds
    last_signature: tuple[Any, ...] | None = None
    while time.monotonic() < deadline:
        try:
            icons = inspect_wechat_taskbar_icons()
            snapshot = {"icons": icons, "count": len(icons)}
            signature = _taskbar_signature(icons)
            if signature != last_signature:
                last_signature = signature
                print("MY_WXAUTO_TASKBAR " + json.dumps(snapshot, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(
                "MY_WXAUTO_TASKBAR "
                + json.dumps({"status": "error", "error": repr(exc)}, ensure_ascii=False),
                flush=True,
            )
        time.sleep(interval)


def watch_unread_wakeup(
    *,
    seconds: float,
    interval: float = 0.25,
    max_controls: int = 160,
    min_changes: int = 4,
    window_seconds: float = 3.0,
    cooldown_seconds: float = 5.0,
    action_timeout: float = 12.0,
    max_probes: int = 1,
    open_unread_messages: bool = False,
) -> None:
    """Watch taskbar/tray flashing, then restore WeChat and dump unread sessions."""

    _ensure_windows()
    detector = TaskbarFlashDetector(
        min_changes=min_changes,
        window_seconds=window_seconds,
        cooldown_seconds=cooldown_seconds,
    )
    deadline = time.monotonic() + seconds
    flash_count = 0
    _emit_wakeup(
        {
            "status": "started",
            "seconds": seconds,
            "interval": interval,
            "max_controls": max_controls,
            "min_changes": min_changes,
            "window_seconds": window_seconds,
            "cooldown_seconds": cooldown_seconds,
            "action_timeout": action_timeout,
            "max_probes": max_probes,
            "open_unread_messages": open_unread_messages,
        }
    )
    while time.monotonic() < deadline:
        try:
            icons = inspect_wechat_taskbar_icons()
            event = detector.observe(time.monotonic(), _taskbar_signature(icons))
            if event is not None:
                flash_count += 1
                _emit_wakeup(
                    {
                        "status": "flash_detected",
                        "flash_index": flash_count,
                        **event,
                        "icons": icons,
                    }
                )
                _emit_wakeup({"status": "probe_started", "flash_index": flash_count})
                sessions = _probe_sessions_after_wakeup_with_timeout(
                    max_controls=max_controls,
                    timeout=action_timeout,
                    restore_icons=icons,
                    open_unread_messages=open_unread_messages,
                    on_progress=lambda progress: _emit_wakeup(
                        {
                            "status": "probe_progress",
                            "flash_index": flash_count,
                            **progress,
                        }
                    ),
                )
                probe_status = str(sessions.get("status") or "ok")
                session_payload = {key: value for key, value in sessions.items() if key != "status"}
                if probe_status == "ok":
                    _emit_wakeup({"status": "sessions", "flash_index": flash_count, **session_payload})
                elif probe_status == "timeout":
                    _emit_wakeup({"status": "probe_timeout", "flash_index": flash_count, **session_payload})
                else:
                    _emit_wakeup({"status": "probe_error", "flash_index": flash_count, **session_payload})
                if max_probes > 0 and flash_count >= max_probes:
                    break
        except Exception as exc:
            _emit_wakeup({"status": "error", "error": repr(exc)})
        time.sleep(interval)
    _emit_wakeup({"status": "finished", "flash_count": flash_count})


def inspect_wechat_taskbar_icons() -> list[dict[str, Any]]:
    icons: list[dict[str, Any]] = []
    for region in _list_taskbar_regions():
        rect = WindowRect(*region["rectangle"])
        item: dict[str, Any] = {
            **region,
        }
        if rect.width > 0 and rect.height > 0:
            try:
                raw = _capture_screen_bgra(rect.left, rect.top, rect.width, rect.height)
                item["image_sha1"] = hashlib.sha1(raw).hexdigest()
                item["red_badges"] = [
                    asdict(badge)
                    for badge in _find_red_components(raw, rect.width, rect.height, origin=(rect.left, rect.top))
                ]
                item["green_icons"] = [
                    asdict(icon)
                    for icon in _find_wechat_green_components(
                        raw,
                        rect.width,
                        rect.height,
                        origin=(rect.left, rect.top),
                    )
                ]
            except Exception as exc:
                item["image_error"] = repr(exc)
        icons.append(item)
    return icons


def _taskbar_signature(icons: Iterable[dict[str, Any]]) -> tuple[Any, ...]:
    return tuple(
        (
            icon.get("source"),
            icon.get("class_name"),
            tuple(icon.get("rectangle", [])),
            icon.get("image_sha1"),
            json.dumps(icon.get("red_badges", []), sort_keys=True, ensure_ascii=False),
        )
        for icon in icons
    )


def _probe_sessions_after_wakeup(
    *,
    max_controls: int,
    restore_icons: list[dict[str, Any]] | None = None,
    open_unread_messages: bool = False,
    max_unread_chats: int = 1,
    max_ui_busy_seconds: float = 15.0,
    on_chat_opened: Callable[[dict[str, Any]], None] | None = None,
    report_progress: Callable[[str, dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    def progress(stage: str, extra: dict[str, Any] | None = None) -> None:
        if report_progress is not None:
            report_progress(stage, extra)

    started = time.perf_counter()
    progress("find_window.before")
    controller = WeChatWindowController(prefer_tray_restore=False)
    window = controller.find_main_window(reveal=True)
    progress("find_window.after", {"window": _window_to_dict(window)})
    if not window.visible and not window.minimized:
        progress("activate.strategy", {"strategy": "ctrl_alt_w_restore"})
        progress("ctrl_alt_w.before", {"hwnd": window.hwnd})
        _send_wechat_toggle_hotkey()
        progress("ctrl_alt_w.after")
        visible_window = _wait_for_visible_window_after_reveal(controller, window)
        progress(
            "ctrl_alt_w.visible_window",
            {"window": _window_to_dict(visible_window) if visible_window is not None else None},
        )
        if visible_window is None:
            raise RuntimeError("Ctrl+Alt+W did not restore the hidden WeChat window.")
        window = visible_window
    else:
        progress("activate.strategy", {"strategy": "win32_restore"})
    progress("activate.before", {"hwnd": window.hwnd})
    activated = controller.activate(window, wait=0.35)
    progress("activate.after", {"window": _window_to_dict(activated)})
    progress("wait_ready.before", {"hwnd": activated.hwnd})
    ready = controller.wait_until_ready(activated, timeout=5.0, stable_for=0.3)
    progress("wait_ready.after", {"window": _window_to_dict(ready)})
    region = _session_list_region(ready)
    progress("collect_uia.before", {"session_region": _rect_to_dict(region), "max_controls": max_controls})
    uia_started = time.perf_counter()
    controls = _collect_uia_controls(ready, region=region, max_controls=max_controls)
    progress(
        "collect_uia.after",
        {
            "duration_ms": round((time.perf_counter() - uia_started) * 1000, 1),
            "control_count": len(controls),
        },
    )
    sessions = _parse_session_items(controls)
    progress("parse_sessions.after", {"session_count": len(sessions)})
    unread_sessions = [session for session in sessions if session.get("has_unread")]
    opened_unread_chats: list[dict[str, Any]] = []
    if open_unread_messages:
        message_targets = unread_sessions
        if not message_targets and sessions:
            top_session = dict(sessions[0])
            top_session["_wakeup_source"] = "top_session_after_flash"
            message_targets = [top_session]
        opened_unread_chats = _open_unread_sessions_and_collect_messages(
            ready,
            message_targets,
            max_controls=max_controls,
            max_unread_chats=max_unread_chats,
            max_ui_busy_seconds=max_ui_busy_seconds,
            on_chat_opened=on_chat_opened,
            report_progress=progress,
        )
    return {
        "window": _window_to_dict(ready),
        "session_region": _rect_to_dict(region),
        "uia": {
            "duration_ms": round((time.perf_counter() - uia_started) * 1000, 1),
            "count": len(controls),
            "controls": controls,
        },
        "sessions": sessions,
        "unread_sessions": unread_sessions,
        "unread_count": len(unread_sessions),
        "opened_unread_chats": opened_unread_chats,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _wait_for_visible_window_after_reveal(
    controller: WeChatWindowController,
    original_window: WeChatWindow,
    *,
    timeout: float = 3.0,
) -> WeChatWindow | None:
    deadline = time.monotonic() + timeout
    latest_visible: WeChatWindow | None = None
    while time.monotonic() < deadline:
        current = controller.get_window(original_window.hwnd)
        if current is not None and _is_visible_normal_window(current):
            return current
        candidates = controller.list_candidate_windows()
        visible_candidates = [candidate for candidate in candidates if _is_visible_normal_window(candidate)]
        if visible_candidates:
            latest_visible = max(visible_candidates, key=lambda item: item.rect.width * item.rect.height)
            return latest_visible
        time.sleep(0.1)
    return latest_visible


def _open_unread_sessions_and_collect_messages(
    window: WeChatWindow,
    unread_sessions: list[dict[str, Any]],
    *,
    max_controls: int,
    max_unread_chats: int = 1,
    max_ui_busy_seconds: float = 15.0,
    on_chat_opened: Callable[[dict[str, Any]], None] | None = None,
    report_progress: Callable[[str, dict[str, Any] | None], None] | None = None,
) -> list[dict[str, Any]]:
    opened: list[dict[str, Any]] = []
    started_at = time.perf_counter()
    limit = max(1, int(max_unread_chats))
    for index, session in enumerate(unread_sessions[:limit]):
        if opened and time.perf_counter() - started_at >= max_ui_busy_seconds:
            if report_progress is not None:
                report_progress(
                    "open_unread.stop_ui_budget",
                    {
                        "index": index,
                        "opened_count": len(opened),
                        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                        "max_ui_busy_seconds": max_ui_busy_seconds,
                    },
                )
            break
        chat_name = str(session.get("chat_name") or "")
        source = str(session.get("_wakeup_source") or "unread_session")
        click_point = _session_click_point(session)
        if report_progress is not None:
            report_progress(
                "open_unread.before",
                {
                    "index": index,
                    "chat_name": chat_name,
                    "source": source,
                    "click_point": list(click_point) if click_point else None,
                },
            )
        if click_point is None:
            opened_chat = {
                "chat_name": chat_name,
                "source": source,
                "status": "no_click_point",
                "unread_count": _session_unread_count(session),
                "messages": [],
            }
            opened.append(opened_chat)
            if report_progress is not None:
                report_progress("open_unread.chat", {"chat": opened_chat})
            if on_chat_opened is not None:
                on_chat_opened(opened_chat)
            continue
        _click_point(click_point)
        time.sleep(0.5)
        if report_progress is not None:
            report_progress("open_unread.after", {"index": index, "chat_name": chat_name, "source": source})

        region = _chat_message_region(window)
        if report_progress is not None:
            report_progress(
                "collect_messages.before",
                {
                    "index": index,
                    "chat_name": chat_name,
                    "source": source,
                    "message_region": _rect_to_dict(region),
                    "max_controls": max_controls,
                },
            )
        started = time.perf_counter()
        controls = _collect_uia_controls(window, region=region, max_controls=max_controls)
        messages = _parse_chat_message_items(controls)
        if report_progress is not None:
            report_progress(
                "collect_messages.after",
                {
                    "index": index,
                    "chat_name": chat_name,
                    "source": source,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "control_count": len(controls),
                    "message_count": len(messages),
                },
            )
        opened_chat = {
            "chat_name": chat_name,
            "source": source,
            "status": "ok",
            "unread_count": _session_unread_count(session),
            "click_point": list(click_point),
            "message_region": _rect_to_dict(region),
            "uia": {
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "count": len(controls),
                "controls": controls,
            },
            "messages": messages,
        }
        opened.append(opened_chat)
        if report_progress is not None:
            report_progress("open_unread.chat", {"chat": opened_chat})
        if on_chat_opened is not None:
            on_chat_opened(opened_chat)
    return opened


def _session_unread_count(session: dict[str, Any]) -> int:
    try:
        return max(0, int(session.get("unread_count") or 0))
    except (TypeError, ValueError):
        return 0


def _session_click_point(session: dict[str, Any]) -> tuple[int, int] | None:
    rect = session.get("rect") or {}
    try:
        left = int(rect["left"])
        top = int(rect["top"])
        right = int(rect["right"])
        bottom = int(rect["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return (left + right) // 2, (top + bottom) // 2


def _click_point(point: tuple[int, int]) -> None:
    import win32api
    import win32con

    x, y = point
    win32api.SetCursorPos((x, y))
    time.sleep(0.04)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
    time.sleep(0.04)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)


def _chat_message_region(window: WeChatWindow) -> WindowRect:
    left = window.rect.left + min(max(window.rect.width // 3, 280), max(280, window.rect.width - 360))
    header = min(max(window.rect.height // 12, 72), 120)
    return WindowRect(
        left=max(window.rect.left, min(left, window.rect.right - 240)),
        top=window.rect.top + header,
        right=window.rect.right,
        bottom=window.rect.bottom,
    )


def _parse_chat_message_items(controls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    current_time_text: str | None = None
    for control in controls:
        control_type = str(control.get("control_type") or "")
        class_name = str(control.get("class_name") or "")
        automation_id = str(control.get("automation_id") or "")
        if control_type != "ListItem":
            continue
        if "ChatSessionCell" in class_name:
            continue
        raw_name = str(control.get("name") or "").strip()
        if "ChatItemView" in class_name and "ChatTextItemView" not in class_name:
            if raw_name:
                current_time_text = raw_name
            continue
        if "chat_message_list" not in automation_id and "ChatTextItemView" not in class_name:
            continue
        if not raw_name:
            continue
        message_type = "text" if "ChatTextItemView" in class_name else "unknown"
        messages.append(
            {
                "content": raw_name,
                "message_type": message_type,
                "sender": None,
                "time_text": current_time_text,
                "raw_name": raw_name,
                "class_name": class_name,
                "automation_id": automation_id,
                "rect": control.get("rect") or {},
            }
        )
    return messages


def _pick_tray_badge_click_point(icons: Iterable[dict[str, Any]]) -> tuple[int, int] | None:
    preferred_boxes: list[tuple[int, int, int, int, int]] = []
    fallback_boxes: list[tuple[int, int, int, int, int]] = []
    for icon in icons:
        rectangle = icon.get("rectangle") or []
        if len(rectangle) != 4:
            continue
        left, top, right, bottom = [int(value) for value in rectangle]
        width = max(0, right - left)
        if width <= 0:
            continue
        tray_left = right - max(260, int(width * 0.18))
        for badge in icon.get("red_badges") or []:
            if not isinstance(badge, dict):
                continue
            badge_rect = badge.get("rect") or {}
            try:
                badge_left = int(badge_rect["left"])
                badge_top = int(badge_rect["top"])
                badge_right = int(badge_rect["right"])
                badge_bottom = int(badge_rect["bottom"])
            except (KeyError, TypeError, ValueError):
                center = badge.get("center")
                if not center or len(center) != 2:
                    continue
                x, y = int(center[0]), int(center[1])
                badge_left = badge_right = x
                badge_top = badge_bottom = y
            center_x = (badge_left + badge_right) // 2
            center_y = (badge_top + badge_bottom) // 2
            if center_y < top or center_y > bottom:
                continue
            box = (badge_left, badge_top, badge_right, badge_bottom, right)
            fallback_boxes.append(box)
            if center_x >= tray_left:
                preferred_boxes.append(box)
    boxes = preferred_boxes or (fallback_boxes if len(fallback_boxes) == 1 else [])
    if not boxes:
        return None

    clusters: list[tuple[int, int, int, int, int]] = []
    for box in sorted(boxes, key=lambda item: (item[4], item[0], item[1])):
        left, top, right, bottom, taskbar_right = box
        if not clusters:
            clusters.append(box)
            continue
        c_left, c_top, c_right, c_bottom, c_taskbar_right = clusters[-1]
        close_horizontally = left <= c_right + 8
        overlaps_vertically = top <= c_bottom + 8 and bottom >= c_top - 8
        same_taskbar = taskbar_right == c_taskbar_right
        if same_taskbar and close_horizontally and overlaps_vertically:
            clusters[-1] = (
                min(c_left, left),
                min(c_top, top),
                max(c_right, right),
                max(c_bottom, bottom),
                c_taskbar_right,
            )
        else:
            clusters.append(box)

    best = sorted(clusters, key=lambda item: (item[4] - item[2], -item[2]))[0]
    left, top, right, bottom, _taskbar_right = best
    return (left + right) // 2, (top + bottom) // 2


def _pick_pixel_restore_click_point(icons: Iterable[dict[str, Any]]) -> tuple[int, int] | None:
    icon_list = list(icons)
    green_point = _pick_wechat_green_click_point(icon_list)
    if green_point is not None:
        return green_point
    return _pick_tray_badge_click_point(icon_list)


def _pick_wechat_green_click_point(icons: Iterable[dict[str, Any]]) -> tuple[int, int] | None:
    candidates: list[tuple[int, int, int, int, int]] = []
    for icon in icons:
        rectangle = icon.get("rectangle") or []
        if len(rectangle) != 4:
            continue
        left, _top, right, _bottom = [int(value) for value in rectangle]
        width = max(0, right - left)
        if width <= 0:
            continue
        tray_left = right - max(260, int(width * 0.18))
        for green_icon in icon.get("green_icons") or []:
            if not isinstance(green_icon, dict):
                continue
            rect = green_icon.get("rect") or {}
            center = green_icon.get("center") or []
            if len(center) != 2:
                continue
            try:
                center_x = int(center[0])
                center_y = int(center[1])
                rect_width = int(rect.get("width") or (int(rect["right"]) - int(rect["left"])))
                rect_height = int(rect.get("height") or (int(rect["bottom"]) - int(rect["top"])))
                area = int(green_icon.get("area") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if center_x < tray_left:
                continue
            if rect_width < 10 or rect_height < 10 or rect_width > 28 or rect_height > 28:
                continue
            square_penalty = abs(rect_width - rect_height)
            candidates.append((center_x, center_y, area, square_penalty, right - center_x))
    if not candidates:
        return None
    center_x, center_y, _area, _square_penalty, _distance = sorted(
        candidates,
        key=lambda item: (item[3], -item[2], item[4]),
    )[0]
    return center_x, center_y


def _double_click_point(point: tuple[int, int]) -> None:
    import win32api
    import win32con

    x, y = point
    win32api.SetCursorPos((x, y))
    for _ in range(2):
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
        time.sleep(0.04)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
        time.sleep(0.08)


def _send_wechat_toggle_hotkey() -> None:
    KeyboardController().hotkey("ctrl", "alt", "w")


def _is_visible_normal_window(window: WeChatWindow) -> bool:
    return (
        window.visible
        and not window.minimized
        and window.rect.width >= 360
        and window.rect.height >= 300
        and window.rect.left > -10000
        and window.rect.top > -10000
    )


def _probe_sessions_after_wakeup_with_timeout(
    *,
    max_controls: int,
    timeout: float,
    restore_icons: list[dict[str, Any]] | None = None,
    open_unread_messages: bool = False,
    max_unread_chats: int = 1,
    max_ui_busy_seconds: float = 15.0,
    on_chat_opened: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    context = mp.get_context("spawn")
    result_queue: mp.Queue[dict[str, Any]] = context.Queue(maxsize=1)
    process = context.Process(
        target=_probe_sessions_after_wakeup_worker,
        args=(
            result_queue,
            max_controls,
            restore_icons,
            open_unread_messages,
            max_unread_chats,
            max_ui_busy_seconds,
        ),
    )
    process.daemon = True
    process.start()
    deadline = time.monotonic() + timeout
    result: dict[str, Any] | None = None
    last_progress: dict[str, Any] | None = None
    completed = False
    try:
        while time.monotonic() < deadline:
            try:
                message = result_queue.get_nowait()
            except queue.Empty:
                if not process.is_alive():
                    break
                time.sleep(0.05)
                continue
            if message.get("status") == "progress":
                last_progress = message
                if message.get("stage") == "open_unread.chat" and on_chat_opened is not None:
                    chat = message.get("chat")
                    if isinstance(chat, dict):
                        on_chat_opened(chat)
                if on_progress is not None:
                    on_progress({key: value for key, value in message.items() if key != "status"})
                continue
            result = message
            break

        if result is None and process.is_alive():
            process.terminate()
            process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join(1.0)
            completed = True
            return {
                "status": "timeout",
                "timeout": timeout,
                "last_progress": {key: value for key, value in (last_progress or {}).items() if key != "status"},
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }

        process.join(1.0)
        if result is None:
            try:
                result = result_queue.get_nowait()
            except queue.Empty:
                result = None
        if result is None:
            completed = True
            return {
                "status": "error",
                "error": f"probe child exited without result, exitcode={process.exitcode}",
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        result["duration_ms"] = result.get("duration_ms", round((time.perf_counter() - started) * 1000, 1))
        completed = True
        return result
    finally:
        if not completed and process.is_alive():
            process.terminate()
            process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join(1.0)


def _probe_sessions_after_wakeup_worker(
    result_queue: mp.Queue[dict[str, Any]],
    max_controls: int,
    restore_icons: list[dict[str, Any]] | None,
    open_unread_messages: bool,
    max_unread_chats: int,
    max_ui_busy_seconds: float,
) -> None:
    def report_progress(stage: str, extra: dict[str, Any] | None = None) -> None:
        result_queue.put({"status": "progress", "stage": stage, **(extra or {})})

    try:
        report_progress("worker.started")
        result_queue.put(
            {
                "status": "ok",
                **_probe_sessions_after_wakeup(
                    max_controls=max_controls,
                    restore_icons=restore_icons,
                    open_unread_messages=open_unread_messages,
                    max_unread_chats=max_unread_chats,
                    max_ui_busy_seconds=max_ui_busy_seconds,
                    report_progress=report_progress,
                ),
            }
        )
    except BaseException as exc:
        result_queue.put({"status": "error", "error": repr(exc)})


def _emit_wakeup(payload: dict[str, Any]) -> None:
    print("MY_WXAUTO_WAKEUP " + json.dumps(payload, ensure_ascii=False), flush=True)


def _list_taskbar_regions() -> list[dict[str, Any]]:
    import win32gui

    class_sources = {
        "Shell_TrayWnd": "primary-taskbar",
        "Shell_SecondaryTrayWnd": "secondary-taskbar",
        "NotifyIconOverflowWindow": "tray-overflow",
    }
    regions: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add_window(hwnd: int, source: str) -> None:
        if not hwnd or hwnd in seen or not win32gui.IsWindow(hwnd):
            return
        seen.add(hwnd)
        rect_tuple = tuple(int(value) for value in win32gui.GetWindowRect(hwnd))
        rect = WindowRect(*rect_tuple)
        if rect.width <= 0 or rect.height <= 0:
            return
        regions.append(
            {
                "source": source,
                "hwnd": hwnd,
                "title": win32gui.GetWindowText(hwnd) or "",
                "class_name": win32gui.GetClassName(hwnd) or "",
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "rectangle": list(rect_tuple),
            }
        )

    for class_name, source in class_sources.items():
        hwnd = win32gui.FindWindow(class_name, None)
        add_window(hwnd, source)

    def callback(hwnd: int, _extra: object) -> bool:
        class_name = win32gui.GetClassName(hwnd) or ""
        source = class_sources.get(class_name)
        if source:
            add_window(hwnd, source)
        return True

    win32gui.EnumWindows(callback, None)
    return regions


def watch_win_events(*, seconds: float) -> None:
    """Print raw WinEvent changes from WeChat processes for discovery."""

    _ensure_windows()
    import win32gui
    import win32process

    controller = WeChatWindowController()
    pids = {
        int(process["pid"])
        for process in controller.list_wechat_processes()
        if str(process.get("name") or "").lower() in {"wechat.exe", "weixin.exe"}
    }
    if not pids:
        raise RuntimeError("未找到 WeChat/Weixin 主进程。")

    event_names = {
        0x0003: "EVENT_SYSTEM_FOREGROUND",
        0x8000: "EVENT_OBJECT_CREATE",
        0x8001: "EVENT_OBJECT_DESTROY",
        0x8002: "EVENT_OBJECT_SHOW",
        0x8003: "EVENT_OBJECT_HIDE",
        0x800A: "EVENT_OBJECT_STATECHANGE",
        0x800B: "EVENT_OBJECT_LOCATIONCHANGE",
        0x800C: "EVENT_OBJECT_NAMECHANGE",
        0x800E: "EVENT_OBJECT_VALUECHANGE",
    }
    user32 = ctypes.windll.user32
    hook_type = ctypes.WINFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_long,
        ctypes.c_long,
        ctypes.c_uint,
        ctypes.c_uint,
    )
    hooks: list[Any] = []

    def _emit_event(
        _hook: Any,
        event: int,
        hwnd: int,
        object_id: int,
        child_id: int,
        event_thread: int,
        event_time: int,
    ) -> None:
        try:
            pid = 0
            if hwnd:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in pids:
                return
            payload = {
                "event": event_names.get(event, hex(event)),
                "event_id": event,
                "hwnd": int(hwnd or 0),
                "object_id": int(object_id),
                "child_id": int(child_id),
                "event_thread": int(event_thread),
                "event_time": int(event_time),
                "window": _hwnd_to_dict(int(hwnd or 0)),
            }
            print("MY_WXAUTO_WINEVENT " + json.dumps(payload, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(
                "MY_WXAUTO_WINEVENT "
                + json.dumps({"status": "error", "error": repr(exc)}, ensure_ascii=False),
                flush=True,
            )

    callback = hook_type(_emit_event)

    def set_hook(event_min: int, event_max: int) -> None:
        for pid in pids:
            hook = user32.SetWinEventHook(
                event_min,
                event_max,
                0,
                callback,
                pid,
                0,
                0x0000 | 0x0002,
            )
            if hook:
                hooks.append(hook)

    set_hook(0x0003, 0x0003)
    set_hook(0x8000, 0x800E)
    print(
        "MY_WXAUTO_WINEVENT "
        + json.dumps(
            {
                "status": "started",
                "pids": sorted(pids),
                "seconds": seconds,
                "hook_count": len(hooks),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        _pump_messages_for(seconds)
    finally:
        for hook in hooks:
            user32.UnhookWinEvent(hook)


def find_red_badges(region: WindowRect) -> list[RedBadgeCandidate]:
    width = region.width
    height = region.height
    if width <= 0 or height <= 0:
        return []
    raw = _capture_screen_bgra(region.left, region.top, width, height)
    return _find_red_components(raw, width, height, origin=(region.left, region.top))


def _find_red_components(
    bgra: bytes,
    width: int,
    height: int,
    *,
    origin: tuple[int, int] = (0, 0),
) -> list[RedBadgeCandidate]:
    red_pixels: set[tuple[int, int]] = set()
    stride = width * 4
    for y in range(height):
        row = y * stride
        for x in range(width):
            i = row + x * 4
            b = bgra[i]
            g = bgra[i + 1]
            r = bgra[i + 2]
            if r >= 170 and g <= 105 and b <= 105 and r - max(g, b) >= 45:
                red_pixels.add((x, y))

    components: list[RedBadgeCandidate] = []
    while red_pixels:
        seed = red_pixels.pop()
        stack = [seed]
        min_x = max_x = seed[0]
        min_y = max_y = seed[1]
        area = 0
        while stack:
            x, y = stack.pop()
            area += 1
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in red_pixels:
                    red_pixels.remove(neighbor)
                    stack.append(neighbor)

        box_width = max_x - min_x + 1
        box_height = max_y - min_y + 1
        if area < 8 or area > 2500:
            continue
        if box_width < 3 or box_height < 3 or box_width > 80 or box_height > 80:
            continue
        abs_left = origin[0] + min_x
        abs_top = origin[1] + min_y
        abs_right = origin[0] + max_x + 1
        abs_bottom = origin[1] + max_y + 1
        components.append(
            RedBadgeCandidate(
                rect={
                    "left": abs_left,
                    "top": abs_top,
                    "right": abs_right,
                    "bottom": abs_bottom,
                    "width": box_width,
                    "height": box_height,
                },
                center=((abs_left + abs_right) // 2, (abs_top + abs_bottom) // 2),
                area=area,
            )
        )
    return sorted(components, key=lambda item: (item.rect["top"], item.rect["left"]))


def _find_wechat_green_components(
    bgra: bytes,
    width: int,
    height: int,
    *,
    origin: tuple[int, int] = (0, 0),
) -> list[RedBadgeCandidate]:
    green_pixels: set[tuple[int, int]] = set()
    stride = width * 4
    for y in range(height):
        row = y * stride
        for x in range(width):
            i = row + x * 4
            b = bgra[i]
            g = bgra[i + 1]
            r = bgra[i + 2]
            if g >= 160 and r <= 170 and b <= 180 and g - max(r, b) >= 20:
                green_pixels.add((x, y))

    components: list[RedBadgeCandidate] = []
    while green_pixels:
        seed = green_pixels.pop()
        stack = [seed]
        min_x = max_x = seed[0]
        min_y = max_y = seed[1]
        area = 0
        while stack:
            x, y = stack.pop()
            area += 1
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in green_pixels:
                    green_pixels.remove(neighbor)
                    stack.append(neighbor)

        box_width = max_x - min_x + 1
        box_height = max_y - min_y + 1
        if area < 20 or area > 1200:
            continue
        if box_width < 6 or box_height < 6 or box_width > 38 or box_height > 38:
            continue
        abs_left = origin[0] + min_x
        abs_top = origin[1] + min_y
        abs_right = origin[0] + max_x + 1
        abs_bottom = origin[1] + max_y + 1
        components.append(
            RedBadgeCandidate(
                rect={
                    "left": abs_left,
                    "top": abs_top,
                    "right": abs_right,
                    "bottom": abs_bottom,
                    "width": box_width,
                    "height": box_height,
                },
                center=((abs_left + abs_right) // 2, (abs_top + abs_bottom) // 2),
                area=area,
            )
        )
    return sorted(components, key=lambda item: (item.rect["top"], item.rect["left"]))


def _parse_session_items(controls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for control in controls:
        automation_id = str(control.get("automation_id") or "")
        class_name = str(control.get("class_name") or "")
        if not automation_id.startswith("session_item_") and "ChatSessionCell" not in class_name:
            continue
        raw_name = str(control.get("name") or "")
        lines = [line.strip() for line in raw_name.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        chat_name = automation_id.removeprefix("session_item_") or (lines[0] if lines else "")
        unread_count = 0
        unread_marker = ""
        preview = ""
        timestamp = ""
        if len(lines) >= 2 and (unread_match := _parse_unread_marker(lines[1])):
            unread_marker = lines[1]
            unread_count = unread_match
            preview = lines[2] if len(lines) >= 3 else ""
            timestamp = lines[3] if len(lines) >= 4 else ""
        else:
            preview = lines[1] if len(lines) >= 2 else ""
            timestamp = lines[2] if len(lines) >= 3 else ""
        sessions.append(
            {
                "chat_name": chat_name,
                "unread_count": unread_count,
                "has_unread": unread_count > 0,
                "unread_marker": unread_marker,
                "preview": preview,
                "time": timestamp,
                "raw_name": raw_name,
                "automation_id": automation_id,
                "rect": control.get("rect") or {},
            }
        )
    return sessions


def _parse_unread_marker(value: str) -> int:
    normalized = value.strip()
    match = re.fullmatch(r"\[(\d+)条\]", normalized)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"(\d+)条", normalized)
    if match:
        return int(match.group(1))
    return 0


def _collect_uia_controls(
    window: WeChatWindow,
    *,
    region: WindowRect,
    max_controls: int,
) -> list[dict[str, Any]]:
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")
    wrapper = desktop.window(handle=window.hwnd)
    controls: list[dict[str, Any]] = []
    for control in wrapper.descendants():
        try:
            rect = control.rectangle()
        except Exception:
            continue
        control_rect = WindowRect(rect.left, rect.top, rect.right, rect.bottom)
        if not _rects_intersect(control_rect, region):
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
                "rect": _rect_to_dict(control_rect),
            }
        )
        if len(controls) >= max_controls:
            break
    return controls


def _capture_screen_bgra(left: int, top: int, width: int, height: int) -> bytes:
    import win32con
    import win32gui
    import win32ui

    desktop_hwnd = win32gui.GetDesktopWindow()
    source_dc = win32gui.GetWindowDC(desktop_hwnd)
    source = win32ui.CreateDCFromHandle(source_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    previous = memory.SelectObject(bitmap)
    try:
        memory.BitBlt((0, 0), (width, height), source, (left, top), win32con.SRCCOPY)
        return bitmap.GetBitmapBits(True)
    finally:
        memory.SelectObject(previous)
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(desktop_hwnd, source_dc)


def _session_list_region(window: WeChatWindow) -> WindowRect:
    left = window.rect.left
    top = window.rect.top
    width = min(max(window.rect.width // 2, 280), 430)
    header = min(max(window.rect.height // 12, 72), 120)
    return WindowRect(
        left=left,
        top=window.rect.top + header,
        right=min(window.rect.right, left + width),
        bottom=window.rect.bottom,
    )


def _snapshot_signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    badges = snapshot.get("red_badges", {}).get("candidates", [])
    controls = snapshot.get("uia", {}).get("controls", [])
    sessions = snapshot.get("sessions", [])
    taskbar_icons = snapshot.get("taskbar", {}).get("icons", [])
    return (
        tuple(
            (
                session.get("chat_name"),
                session.get("unread_count"),
                session.get("preview"),
                session.get("time"),
            )
            for session in sessions
        ),
        tuple(
            (
                badge.get("rect", {}).get("left"),
                badge.get("rect", {}).get("top"),
                badge.get("rect", {}).get("right"),
                badge.get("rect", {}).get("bottom"),
                badge.get("area"),
            )
            for badge in badges
        ),
        tuple(
            (
                control.get("name"),
                control.get("control_type"),
                tuple(control.get("rect", {}).get(key) for key in ("left", "top", "right", "bottom")),
            )
            for control in controls
        ),
        tuple(
            (
                icon.get("name"),
                icon.get("source"),
                tuple(icon.get("rectangle", [])),
                icon.get("image_sha1"),
            )
            for icon in taskbar_icons
        ),
    )


def _pump_messages_for(seconds: float) -> None:
    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class Msg(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", ctypes.c_void_p),
            ("lParam", ctypes.c_void_p),
            ("time", ctypes.c_uint),
            ("pt", Point),
        ]

    user32 = ctypes.windll.user32
    deadline = time.monotonic() + seconds
    msg = Msg()
    while time.monotonic() < deadline:
        while user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 0x0001):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.03)


def _hwnd_to_dict(hwnd: int) -> dict[str, Any] | None:
    if not hwnd:
        return None
    try:
        import psutil
        import win32gui
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process_name = ""
        try:
            process_name = psutil.Process(pid).name()
        except Exception:
            pass
        rect = win32gui.GetWindowRect(hwnd)
        return {
            "title": win32gui.GetWindowText(hwnd) or "",
            "class_name": win32gui.GetClassName(hwnd) or "",
            "pid": pid,
            "process_name": process_name,
            "rect": list(rect),
        }
    except Exception:
        return None


def _window_to_dict(window: WeChatWindow) -> dict[str, Any]:
    return {
        "hwnd": window.hwnd,
        "title": window.title,
        "class_name": window.class_name,
        "pid": window.pid,
        "process_name": window.process_name,
        "visible": window.visible,
        "minimized": window.minimized,
        "rect": _rect_to_dict(window.rect),
    }


def _rect_to_dict(rect: WindowRect) -> dict[str, int]:
    return {
        "left": rect.left,
        "top": rect.top,
        "right": rect.right,
        "bottom": rect.bottom,
        "width": rect.width,
        "height": rect.height,
    }


def _rects_intersect(a: WindowRect, b: WindowRect) -> bool:
    return a.left < b.right and a.right > b.left and a.top < b.bottom and a.bottom > b.top


def _contains_any(value: str, keywords: Iterable[str]) -> bool:
    folded = value.casefold()
    return any(keyword.casefold() in folded for keyword in keywords)


def _ensure_windows() -> None:
    if platform.system() != "Windows":
        raise OSError("my_wxauto 诊断探针目前只支持 Windows。")
