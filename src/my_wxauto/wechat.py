from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .bridge_store import BridgeStore
from .debug_trace import make_ui_tracer
from .keyboard import KeyboardController
from .response import WxResponse
from .window import WeChatWindow, WeChatWindowController
from .wxauto4_backend import Wxauto4Backend

LOGGER = logging.getLogger(__name__)
SEARCH_RESULT_SECTION_LABELS = ("联系人", "群聊")


@dataclass(frozen=True)
class SearchOptions:
    search_box_offset: tuple[int, int] = (120, 55)
    wxauto4_avatar_offset: tuple[int, int] = (120, 55)
    wxauto4_construct_cleanup_delay: float = 0.7
    wxauto4_construct_cleanup_timeout: float = 4.0
    wxauto4_construct_cleanup_interval: float = 0.15
    search_shortcut: tuple[str, ...] = ("ctrl", "f")
    use_shortcut: bool = True
    use_click: bool = False
    close_avatar_after_wxauto4: bool = True
    result_wait: float = 0.65
    chat_open_wait: float = 0.12
    window_ready_wait: float = 0.0
    window_ready_timeout: float = 5.0
    shortcut_first_when_recovered: bool = True
    restore_clipboard: bool = True
    search_down_count: int = 0
    search_down_interval: float = 0.06
    prefer_exact_search_result_click: bool = True


@dataclass(frozen=True)
class _SearchResultControl:
    name: str
    rect: dict[str, int]


class WeChat:
    def __init__(
        self,
        *,
        window_controller: WeChatWindowController | None = None,
        keyboard: KeyboardController | None = None,
        search_options: SearchOptions | None = None,
        debug: bool = False,
        trace_ui: bool = False,
        prefer_wxauto4: bool = True,
        bridge_store_path: str | Path = ".my_wxauto_bridge.sqlite3",
        **_ignored_compat_kwargs: object,
    ) -> None:
        self.window_controller = window_controller
        self.keyboard = keyboard
        self.search_options = search_options or SearchOptions()
        self.debug = debug
        self.trace = make_ui_tracer(trace_ui)
        self.prefer_wxauto4 = prefer_wxauto4 and window_controller is None and keyboard is None
        self.bridge_store_path = bridge_store_path
        self._avatar_cleanup_lock = threading.Lock()
        self._wxauto4_avatar_cleanup_done = False
        self._wxauto4_backend = (
            Wxauto4Backend(
                trace=self.trace,
                before_construct=self._start_wxauto4_construct_cleanup_watchdog,
                debug=debug,
                **_ignored_compat_kwargs,
            )
            if self.prefer_wxauto4
            else None
        )
        if debug:
            logging.basicConfig(level=logging.DEBUG)

    def ChatWith(
        self,
        who: str,
        exact: bool = True,
        force: bool = False,
        force_wait: float | int = 0.5,
    ) -> WxResponse:
        """Open a WeChat conversation by searching its display name.

        The WeChat 4.x UI does not expose enough stable UIAutomation metadata for
        strict result matching, so the first implementation opens the first search
        result and reports the match as unverified.
        """

        target = self._normalize_target(who)
        if not target:
            return WxResponse.failure("聊天对象不能为空。")

        try:
            self.trace("chatwith.enter", target=target)
            window, backend = self._open_chat_window(target, force=force, force_wait=force_wait)
            self.trace("chatwith.after_open_chat_window", backend=backend)
            return WxResponse.success(
                f"已尝试打开聊天框: {target}",
                {
                    "who": target,
                    "exact_requested": exact,
                    "match_verified": False,
                    "backend": backend,
                    "strategy": self._strategy_name(),
                    "window": self._window_data(window),
                },
            )
        except Exception as exc:
            if self.debug:
                LOGGER.exception("ChatWith failed.")
            else:
                LOGGER.debug("ChatWith failed.", exc_info=True)
            return WxResponse.error(f"打开聊天框失败: {exc}", {"who": target})

    def SendMsg(
        self,
        msg: str,
        who: str,
        exact: bool = True,
        force: bool = False,
        force_wait: float | int = 0.5,
    ) -> WxResponse:
        target = self._normalize_target(who)
        content = self._normalize_message(msg)
        if not target:
            return WxResponse.failure("聊天对象不能为空。")
        if not content:
            return WxResponse.failure("发送内容不能为空。", {"who": target})

        try:
            window, backend = self._open_chat_window(target, force=force, force_wait=force_wait)
            keyboard = self._keyboard
            keyboard.paste_text(
                content,
                restore_clipboard=self.search_options.restore_clipboard,
            )
            keyboard.press("enter")
            self._record_outgoing_echo(target, content)
            return WxResponse.success(
                f"已尝试向 {target} 发送消息。",
                {
                    "who": target,
                    "message": content,
                    "message_length": len(content),
                    "exact_requested": exact,
                    "match_verified": False,
                    "backend": backend,
                    "strategy": self._strategy_name(),
                    "window": self._window_data(window),
                },
            )
        except Exception as exc:
            if self.debug:
                LOGGER.exception("SendMsg failed.")
            else:
                LOGGER.debug("SendMsg failed.", exc_info=True)
            return WxResponse.error(
                f"发送消息失败: {exc}",
                {"who": target, "message": content},
            )

    def open_chat(self, who: str, **kwargs: object) -> WxResponse:
        return self.ChatWith(who, **kwargs)

    def send_message(self, who: str, message: str, **kwargs: object) -> WxResponse:
        return self.SendMsg(message, who, **kwargs)

    def listen_new_messages(self, callback, **kwargs: object):
        from . import listener

        return listener.listen_new_messages(callback, **kwargs)

    def listen_conversation_batches(self, callback, **kwargs: object):
        from . import listener

        return listener.listen_conversation_batches(callback, **kwargs)

    def get_latest_message(self, who: str, **kwargs: object):
        from . import listener

        open_first = bool(kwargs.pop("open_first", True))
        return listener.get_latest_message(who, open_chat=self.ChatWith if open_first else None, **kwargs)

    def GetLatestMessage(self, who: str, **kwargs: object):
        return self.get_latest_message(who, **kwargs)

    def get_visible_messages(self, who: str, **kwargs: object):
        from . import listener

        open_first = bool(kwargs.pop("open_first", True))
        return listener.get_visible_messages(who, open_chat=self.ChatWith if open_first else None, **kwargs)

    def GetVisibleMessages(self, who: str, **kwargs: object):
        return self.get_visible_messages(who, **kwargs)

    def _open_chat_window(
        self,
        target: str,
        *,
        force: bool,
        force_wait: float | int,
    ) -> tuple[WeChatWindow | None, str]:
        self.trace("open_chat_window.enter", target=target)
        if self._prepare_window_with_wxauto4():
            self._search_from_focused_window(target, force=force, force_wait=force_wait)
            self.trace("open_chat_window.after_shortcut_search")
            return None, "wxauto4-restore+shortcut-search"
        window_controller = self._window_controller
        keyboard = self._keyboard

        window = window_controller.find_main_window()
        recovered_mode = (
            window.recovered_from_process
            or window.minimized
            or not window.visible
            or window.rect.left <= -10000
            or window.rect.top <= -10000
        )
        activated_window = window_controller.activate(window)
        if activated_window is not None:
            window = activated_window
        if window.recovered_from_tray:
            recovered_mode = False
        elif window.recovered_from_process:
            recovered_mode = True
        if self.search_options.window_ready_wait > 0:
            window = window_controller.wait_until_ready(
                window,
                timeout=self.search_options.window_ready_timeout,
                min_wait=self.search_options.window_ready_wait,
            )
        self._focus_search_box(window, shortcut_first=recovered_mode)
        keyboard.select_all()
        keyboard.paste_text(
            target,
            restore_clipboard=self.search_options.restore_clipboard,
        )
        wait_seconds = float(force_wait) if force else self.search_options.result_wait
        time.sleep(wait_seconds)
        self._open_selected_search_result_or_press_enter(window, target)
        time.sleep(self.search_options.chat_open_wait)
        return window, "window-controller"

    def _search_from_focused_window(
        self,
        target: str,
        *,
        force: bool,
        force_wait: float | int,
    ) -> None:
        keyboard = self._keyboard
        self.trace("shortcut_search.before_focus_search", target=target)
        if self.search_options.use_shortcut and self.search_options.search_shortcut:
            self.trace("shortcut_search.before_hotkey", keys=self.search_options.search_shortcut)
            keyboard.hotkey(*self.search_options.search_shortcut)
            self.trace("shortcut_search.after_hotkey", keys=self.search_options.search_shortcut)
            time.sleep(0.08)
        else:
            self.trace("shortcut_search.before_hotkey", keys=("ctrl", "f"))
            keyboard.hotkey("ctrl", "f")
            self.trace("shortcut_search.after_hotkey", keys=("ctrl", "f"))
            time.sleep(0.08)
        self.trace("shortcut_search.before_select_all")
        keyboard.select_all()
        self.trace("shortcut_search.after_select_all")
        self.trace("shortcut_search.before_paste", target=target)
        keyboard.paste_text(
            target,
            restore_clipboard=self.search_options.restore_clipboard,
        )
        self.trace("shortcut_search.after_paste")
        wait_seconds = float(force_wait) if force else self.search_options.result_wait
        time.sleep(wait_seconds)
        window = self._focused_search_window()
        self._open_selected_search_result_or_press_enter(window, target)
        time.sleep(self.search_options.chat_open_wait)

    def _open_selected_search_result_or_press_enter(
        self,
        window: WeChatWindow | None,
        target: str,
    ) -> None:
        if self._click_exact_search_result(window, target):
            return

        keyboard = self._keyboard
        down_count = self.search_options.search_down_count
        for _ in range(down_count):
            keyboard.press("down")
            time.sleep(self.search_options.search_down_interval)
        self.trace("shortcut_search.before_enter", down_count=down_count)
        keyboard.press("enter")
        self.trace("shortcut_search.after_enter")

    def _click_exact_search_result(self, window: WeChatWindow | None, target: str) -> bool:
        if window is None or not self.search_options.prefer_exact_search_result_click:
            return False
        try:
            controls = tuple(self._collect_search_result_controls(window))
        except Exception:
            LOGGER.debug("Failed to collect search result controls.", exc_info=True)
            return False
        control = _pick_exact_search_result_control(controls, target)
        if control is None:
            self.trace("search_result.no_exact_click", target=target, controls=len(controls))
            return False
        point = _rect_center(control.rect)
        if point is None:
            return False
        self.trace("search_result.before_click", target=target, point=point, name=control.name)
        self._keyboard.click(*point)
        self.trace("search_result.after_click", target=target, point=point, name=control.name)
        return True

    def _focused_search_window(self) -> WeChatWindow | None:
        try:
            return self._window_controller.find_main_window(reveal=False)
        except Exception:
            LOGGER.debug("Unable to resolve focused WeChat window for search results.", exc_info=True)
            return None

    def _collect_search_result_controls(self, window: WeChatWindow) -> tuple[_SearchResultControl, ...]:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        wrapper = desktop.window(handle=window.hwnd)
        panel_right = min(window.rect.right, window.rect.left + 420)
        panel_top = window.rect.top + 48
        controls: list[_SearchResultControl] = []
        for control in wrapper.descendants():
            try:
                rect = control.rectangle()
            except Exception:
                continue
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            if rect.right < window.rect.left or rect.left > panel_right:
                continue
            if rect.bottom < panel_top or rect.top > window.rect.bottom:
                continue
            element_info = getattr(control, "element_info", None)
            name = str(getattr(element_info, "name", "") or "").strip()
            if not name:
                continue
            controls.append(
                _SearchResultControl(
                    name=name,
                    rect={
                        "left": int(rect.left),
                        "top": int(rect.top),
                        "right": int(rect.right),
                        "bottom": int(rect.bottom),
                    },
                )
            )
        return tuple(controls)

    def _focus_search_box(self, window: WeChatWindow, *, shortcut_first: bool = False) -> None:
        options = self.search_options
        keyboard = self._keyboard
        if shortcut_first and options.shortcut_first_when_recovered and options.use_shortcut:
            keyboard.hotkey(*options.search_shortcut)
            time.sleep(0.08)
            return

        if options.use_click:
            x, y = window.point(options.search_box_offset)
            keyboard.click(x, y)
            time.sleep(0.08)

        if options.use_shortcut and options.search_shortcut:
            keyboard.hotkey(*options.search_shortcut)
            time.sleep(0.08)

    def _try_wxauto4_chat_with(
        self,
        target: str,
        *,
        exact: bool,
        force: bool,
        force_wait: float | int,
    ) -> WxResponse | None:
        if self._wxauto4_backend is None:
            return None
        result = self._wxauto4_backend.chat_with(
            target,
            exact=exact,
            force=force,
            force_wait=force_wait,
        )
        if not result.ok:
            if self.debug:
                LOGGER.debug("wxauto4 ChatWith failed: %s", result.error)
            return None
        return WxResponse.success(
            f"已通过 wxauto4 打开聊天框: {target}",
            {
                "who": target,
                "exact_requested": exact,
                "match_verified": False,
                "backend": "wxauto4",
                "raw_result": self._safe_raw_result(result.value),
            },
        )

    def _prepare_window_with_wxauto4(self) -> bool:
        if self._wxauto4_backend is None:
            return False
        self.trace("prepare_window_with_wxauto4.before")
        result = self._wxauto4_backend.prepare_window()
        self.trace(
            "prepare_window_with_wxauto4.after",
            ok=result.ok,
            value=result.value,
            error=repr(result.error),
        )
        if not result.ok:
            self._close_avatar_popover_after_wxauto4()
            if self.debug:
                LOGGER.debug("wxauto4 prepare_window failed: %s", result.error)
            return False
        if not self._close_avatar_popover_after_wxauto4():
            return False
        time.sleep(0.2)
        return True

    def _start_wxauto4_construct_cleanup_watchdog(self) -> None:
        if not self.search_options.close_avatar_after_wxauto4:
            return
        thread = threading.Thread(
            target=self._run_wxauto4_construct_cleanup_watchdog,
            name="my-wxauto-wxauto4-avatar-cleanup",
            daemon=True,
        )
        thread.start()
        self.trace(
            "wxauto4.avatar_watchdog.started",
            delay=self.search_options.wxauto4_construct_cleanup_delay,
            timeout=self.search_options.wxauto4_construct_cleanup_timeout,
        )

    def _run_wxauto4_construct_cleanup_watchdog(self) -> None:
        options = self.search_options
        try:
            if options.wxauto4_construct_cleanup_delay > 0:
                time.sleep(options.wxauto4_construct_cleanup_delay)

            deadline = time.monotonic() + options.wxauto4_construct_cleanup_timeout
            while time.monotonic() < deadline:
                popup = self._find_wechat_transient_popup()
                if popup is not None:
                    self.trace("wxauto4.avatar_watchdog.popup_detected", popup=popup)
                    if self._click_avatar_popover_once("wxauto4.avatar_watchdog"):
                        return
                time.sleep(options.wxauto4_construct_cleanup_interval)
            self.trace("wxauto4.avatar_watchdog.timeout")
        except Exception as exc:
            self.trace("wxauto4.avatar_watchdog.failed", error=repr(exc))

    def _close_avatar_popover_after_wxauto4(self) -> bool:
        if not self.search_options.close_avatar_after_wxauto4:
            return True
        return self._click_avatar_popover_once("wxauto4.avatar_cleanup")

    def _click_avatar_popover_once(self, label_prefix: str) -> bool:
        with self._avatar_cleanup_lock:
            if self._wxauto4_avatar_cleanup_done:
                self.trace(f"{label_prefix}.skipped_already_done")
                return True

        try:
            window = self._window_controller.find_main_window(reveal=False)
        except Exception as exc:
            self.trace(f"{label_prefix}.no_window", error=repr(exc))
            return False

        try:
            x, y = window.point(self.search_options.wxauto4_avatar_offset)
            self.trace(f"{label_prefix}.before_click", point=(x, y))
            self._keyboard.click(x, y)
            self.trace(f"{label_prefix}.after_click", point=(x, y))
            time.sleep(0.12)
            self.trace(f"{label_prefix}.before_esc")
            self._keyboard.press("esc")
            self.trace(f"{label_prefix}.after_esc")
            time.sleep(0.08)
            self._wxauto4_avatar_cleanup_done = True
            return True
        except Exception as exc:
            self.trace(f"{label_prefix}.failed", error=repr(exc))
            if self.debug:
                LOGGER.debug("Unable to close wxauto4 avatar popover.", exc_info=True)
            return False

    def _find_wechat_transient_popup(self) -> dict[str, object] | None:
        try:
            import psutil
            import win32gui
            import win32process
        except Exception as exc:
            self.trace("wxauto4.avatar_watchdog.import_failed", error=repr(exc))
            return None

        def popup_from_hwnd(hwnd: int) -> dict[str, object] | None:
            if not hwnd or not win32gui.IsWindowVisible(hwnd):
                return None
            class_name = win32gui.GetClassName(hwnd) or ""
            if "ToolSaveBits" not in class_name:
                return None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = ""
            try:
                process_name = psutil.Process(pid).name() or ""
            except Exception:
                pass
            if process_name.lower() not in {"wechat.exe", "weixin.exe"}:
                return None
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                return None
            return {
                "hwnd": hwnd,
                "title": win32gui.GetWindowText(hwnd) or "",
                "class_name": class_name,
                "pid": pid,
                "process_name": process_name,
                "rect": list(rect),
            }

        try:
            foreground_popup = popup_from_hwnd(win32gui.GetForegroundWindow())
            if foreground_popup is not None:
                return foreground_popup

            matches: list[dict[str, object]] = []

            def callback(hwnd: int, _extra: object) -> bool:
                popup = popup_from_hwnd(hwnd)
                if popup is not None:
                    matches.append(popup)
                    return False
                return True

            win32gui.EnumWindows(callback, None)
            return matches[0] if matches else None
        except Exception as exc:
            self.trace("wxauto4.avatar_watchdog.scan_failed", error=repr(exc))
            return None

    def _try_wxauto4_send_msg(
        self,
        content: str,
        target: str,
        *,
        exact: bool,
        force: bool,
        force_wait: float | int,
    ) -> WxResponse | None:
        if self._wxauto4_backend is None:
            return None
        result = self._wxauto4_backend.send_msg(
            content,
            target,
            exact=exact,
            force=force,
            force_wait=force_wait,
        )
        if not result.ok:
            if self.debug:
                LOGGER.debug("wxauto4 SendMsg failed: %s", result.error)
            return None
        return WxResponse.success(
            f"已通过 wxauto4 向 {target} 发送消息。",
            {
                "who": target,
                "message": content,
                "message_length": len(content),
                "exact_requested": exact,
                "match_verified": False,
                "backend": "wxauto4",
                "raw_result": self._safe_raw_result(result.value),
            },
        )

    @property
    def _window_controller(self) -> WeChatWindowController:
        if self.window_controller is None:
            self.window_controller = WeChatWindowController()
        return self.window_controller

    @property
    def _keyboard(self) -> KeyboardController:
        if self.keyboard is None:
            self.keyboard = KeyboardController()
        return self.keyboard

    def _strategy_name(self) -> str:
        options = self.search_options
        parts: list[str] = []
        if options.use_click:
            parts.append(f"click@{options.search_box_offset[0]},{options.search_box_offset[1]}")
        if options.use_shortcut:
            parts.append("+".join(options.search_shortcut))
        return " then ".join(parts) or "direct"

    def _window_data(self, window: WeChatWindow | None) -> dict[str, object] | None:
        if window is None:
            return None
        return {
            "hwnd": window.hwnd,
            "title": window.title,
            "class_name": window.class_name,
            "pid": window.pid,
            "process_name": window.process_name,
            "visible": window.visible,
            "minimized": window.minimized,
            "recovered_from_process": window.recovered_from_process,
            "recovered_from_tray": window.recovered_from_tray,
            "rect": {
                "left": window.rect.left,
                "top": window.rect.top,
                "right": window.rect.right,
                "bottom": window.rect.bottom,
            },
        }

    def _normalize_target(self, who: str) -> str:
        return str(who or "").strip()

    def _normalize_message(self, msg: str) -> str:
        return str(msg or "").strip()

    def _safe_raw_result(self, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool, list, tuple, dict)):
            return value
        return repr(value)

    def _record_outgoing_echo(self, target: str, content: str) -> None:
        try:
            BridgeStore(self.bridge_store_path).record_outgoing_echo(target, content, sent_at=time.time())
        except Exception:
            LOGGER.debug("Unable to record outgoing echo.", exc_info=True)


def _pick_exact_search_result_control(
    controls: Sequence[object],
    target: str,
) -> object | None:
    normalized_target = _normalize_search_result_text(target)
    if not normalized_target:
        return None

    sections = [
        control
        for control in controls
        if _normalize_search_result_text(getattr(control, "name", "")) in SEARCH_RESULT_SECTION_LABELS
    ]
    exact_matches = [
        control
        for control in controls
        if _search_result_primary_name(str(getattr(control, "name", ""))) == normalized_target
    ]
    if not exact_matches:
        return None

    section_matches = [
        match
        for match in exact_matches
        if any(_rect_top(match) >= _rect_bottom(section) - 2 for section in sections)
    ]
    if section_matches:
        return min(section_matches, key=_rect_top)

    if len(exact_matches) == 1:
        return exact_matches[0]
    return None


def _search_result_primary_name(value: str) -> str:
    for line in str(value or "").splitlines():
        normalized = _normalize_search_result_text(line)
        if normalized:
            return normalized
    return ""


def _normalize_search_result_text(value: str) -> str:
    return str(value or "").strip()


def _rect_top(control: object) -> int:
    rect = getattr(control, "rect", {}) or {}
    try:
        return int(rect["top"])
    except (KeyError, TypeError, ValueError):
        return 0


def _rect_bottom(control: object) -> int:
    rect = getattr(control, "rect", {}) or {}
    try:
        return int(rect["bottom"])
    except (KeyError, TypeError, ValueError):
        return 0


def _rect_center(rect: dict[str, int]) -> tuple[int, int] | None:
    try:
        left = int(rect["left"])
        top = int(rect["top"])
        right = int(rect["right"])
        bottom = int(rect["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return ((left + right) // 2, (top + bottom) // 2)
