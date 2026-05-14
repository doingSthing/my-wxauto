from __future__ import annotations

import contextlib
import platform
import time
from collections.abc import Iterator

from .exceptions import ClipboardError

VK = {
    "ctrl": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "enter": 0x0D,
    "esc": 0x1B,
    "tab": 0x09,
    "down": 0x28,
    "a": 0x41,
    "f": 0x46,
    "k": 0x4B,
    "v": 0x56,
    "w": 0x57,
}


class KeyboardController:
    def __init__(self, key_interval: float = 0.05, paste_wait: float = 0.08) -> None:
        self.key_interval = key_interval
        self.paste_wait = paste_wait

    def hotkey(self, *keys: str) -> None:
        self._ensure_windows()
        import win32api
        import win32con

        key_codes = [self._vk(key) for key in keys]
        for code in key_codes:
            win32api.keybd_event(code, 0, 0, 0)
            time.sleep(self.key_interval)
        for code in reversed(key_codes):
            win32api.keybd_event(code, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(self.key_interval)

    def press(self, key: str) -> None:
        self.hotkey(key)

    def click(self, x: int, y: int) -> None:
        self._ensure_windows()
        import win32api
        import win32con

        win32api.SetCursorPos((x, y))
        time.sleep(self.key_interval)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(self.key_interval)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def paste_text(self, text: str, restore_clipboard: bool = True) -> None:
        with windows_clipboard_text(text, restore=restore_clipboard):
            self.hotkey("ctrl", "v")
            time.sleep(self.paste_wait)

    def select_all(self) -> None:
        self.hotkey("ctrl", "a")

    def _vk(self, key: str) -> int:
        normalized = key.lower()
        if normalized not in VK:
            raise ValueError(f"Unsupported key: {key}")
        return VK[normalized]

    def _ensure_windows(self) -> None:
        if platform.system() != "Windows":
            raise OSError("my_wxauto 目前只支持 Windows 微信客户端。")


@contextlib.contextmanager
def windows_clipboard_text(text: str, restore: bool = True) -> Iterator[None]:
    if platform.system() != "Windows":
        raise OSError("Windows clipboard is required.")

    import win32clipboard
    import win32con

    previous_text: str | None = None
    had_text = False

    try:
        _open_clipboard_with_retry(win32clipboard)
        try:
            had_text = win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT)
            if had_text:
                previous_text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()

        yield
    finally:
        if restore:
            try:
                _open_clipboard_with_retry(win32clipboard)
                try:
                    win32clipboard.EmptyClipboard()
                    if had_text and previous_text is not None:
                        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, previous_text)
                finally:
                    win32clipboard.CloseClipboard()
            except Exception as exc:
                raise ClipboardError(f"恢复剪贴板失败: {exc}") from exc


def _open_clipboard_with_retry(win32clipboard_module: object, attempts: int = 10) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            win32clipboard_module.OpenClipboard()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.05)
    raise ClipboardError(f"打开剪贴板失败: {last_error}") from last_error
