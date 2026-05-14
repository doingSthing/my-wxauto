from __future__ import annotations

from my_wxauto.bridge_store import BridgeStore
from my_wxauto.wechat import SearchOptions, WeChat
from my_wxauto.window import WeChatWindow, WindowRect
from my_wxauto.wxauto4_backend import Wxauto4Backend, Wxauto4CallResult


class FakeWindowController:
    def __init__(self) -> None:
        self.window = WeChatWindow(
            hwnd=100,
            title="微信",
            class_name="Qt51514QWindowIcon",
            pid=42,
            process_name="WeChat.exe",
            exe=r"C:\Program Files\Tencent\WeChat\WeChat.exe",
            rect=WindowRect(10, 20, 900, 700),
        )
        self.activated = False

    def find_main_window(self, reveal: bool = True) -> WeChatWindow:
        return self.window

    def activate(self, window: WeChatWindow) -> None:
        assert window == self.window
        self.activated = True

    def wait_until_ready(
        self,
        window: WeChatWindow,
        *,
        timeout: float = 5.0,
        stable_for: float = 0.5,
        min_wait: float = 0.0,
    ) -> WeChatWindow:
        return window


class RefreshingWindowController(FakeWindowController):
    def __init__(self) -> None:
        super().__init__()
        self.window = WeChatWindow(
            hwnd=100,
            title="WeChat",
            class_name="Qt51514QWindowIcon",
            pid=42,
            process_name="WeChat.exe",
            exe=r"C:\Program Files\Tencent\WeChat\WeChat.exe",
            rect=WindowRect(-32000, -32000, -31840, -31960),
            visible=True,
            minimized=True,
            recovered_from_process=True,
        )
        self.restored_window = WeChatWindow(
            hwnd=100,
            title="WeChat",
            class_name="Qt51514QWindowIcon",
            pid=42,
            process_name="WeChat.exe",
            exe=r"C:\Program Files\Tencent\WeChat\WeChat.exe",
            rect=WindowRect(30, 40, 900, 700),
            visible=True,
            minimized=False,
        )

    def activate(self, window: WeChatWindow) -> WeChatWindow:
        super().activate(window)
        return self.restored_window


class TrayRestoredWindowController(RefreshingWindowController):
    def __init__(self) -> None:
        super().__init__()
        self.restored_window = WeChatWindow(
            hwnd=100,
            title="WeChat",
            class_name="Qt51514QWindowIcon",
            pid=42,
            process_name="WeChat.exe",
            exe=r"C:\Program Files\Tencent\WeChat\WeChat.exe",
            rect=WindowRect(30, 40, 900, 700),
            visible=True,
            minimized=False,
            recovered_from_tray=True,
        )


class FakeKeyboard:
    def __init__(self) -> None:
        self.actions: list[tuple[str, object]] = []

    def click(self, x: int, y: int) -> None:
        self.actions.append(("click", (x, y)))

    def hotkey(self, *keys: str) -> None:
        self.actions.append(("hotkey", keys))

    def select_all(self) -> None:
        self.actions.append(("select_all", None))

    def paste_text(self, text: str, restore_clipboard: bool = True) -> None:
        self.actions.append(("paste", (text, restore_clipboard)))

    def press(self, key: str) -> None:
        self.actions.append(("press", key))


class FakeWxauto4Backend:
    def prepare_window(self) -> Wxauto4CallResult:
        return Wxauto4CallResult(ok=True, value="init")


def test_chatwith_searches_and_presses_enter() -> None:
    windows = FakeWindowController()
    keyboard = FakeKeyboard()
    wx = WeChat(
        window_controller=windows,
        keyboard=keyboard,
        search_options=SearchOptions(result_wait=0, search_box_offset=(120, 55), window_ready_wait=0),
    )

    result = wx.ChatWith(" 张三 ")

    assert result
    assert windows.activated
    assert result.data["who"] == "张三"
    assert result.data["match_verified"] is False
    assert keyboard.actions == [
        ("hotkey", ("ctrl", "f")),
        ("select_all", None),
        ("paste", ("张三", True)),
        ("press", "enter"),
    ]


def test_chatwith_can_disable_shortcut() -> None:
    keyboard = FakeKeyboard()
    wx = WeChat(
        window_controller=FakeWindowController(),
        keyboard=keyboard,
        search_options=SearchOptions(use_shortcut=False, result_wait=0),
    )

    result = wx.ChatWith("文件传输助手")

    assert result
    assert ("hotkey", ("ctrl", "f")) not in keyboard.actions
    assert keyboard.actions[-1] == ("press", "enter")


def test_chatwith_can_move_down_before_opening_search_result() -> None:
    keyboard = FakeKeyboard()
    wx = WeChat(
        window_controller=FakeWindowController(),
        keyboard=keyboard,
        search_options=SearchOptions(result_wait=0, search_down_count=2, search_down_interval=0),
    )

    result = wx.ChatWith("文件传输助手")

    assert result
    assert keyboard.actions[-3:] == [
        ("press", "down"),
        ("press", "down"),
        ("press", "enter"),
    ]


def test_chatwith_rejects_empty_target() -> None:
    wx = WeChat(window_controller=FakeWindowController(), keyboard=FakeKeyboard())

    result = wx.ChatWith("   ")

    assert not result
    assert result.status == "failure"


def test_wxauto4_restore_closes_avatar_popover_before_search() -> None:
    windows = FakeWindowController()
    keyboard = FakeKeyboard()
    wx = WeChat(
        window_controller=windows,
        keyboard=keyboard,
        search_options=SearchOptions(result_wait=0, chat_open_wait=0),
    )
    wx._wxauto4_backend = FakeWxauto4Backend()

    result = wx.ChatWith("张三")

    assert result
    assert result.data["backend"] == "wxauto4-restore+shortcut-search"
    assert keyboard.actions == [
        ("click", (130, 75)),
        ("press", "esc"),
        ("hotkey", ("ctrl", "f")),
        ("select_all", None),
        ("paste", ("张三", True)),
        ("press", "enter"),
    ]


def test_chatwith_uses_shortcut_first_after_recovering_blank_shell() -> None:
    windows = RefreshingWindowController()
    keyboard = FakeKeyboard()
    wx = WeChat(
        window_controller=windows,
        keyboard=keyboard,
        search_options=SearchOptions(result_wait=0, search_box_offset=(120, 55), window_ready_wait=0),
    )

    result = wx.ChatWith("restored")

    assert result
    assert keyboard.actions[:4] == [
        ("hotkey", ("ctrl", "f")),
        ("select_all", None),
        ("paste", ("restored", True)),
        ("press", "enter"),
    ]


def test_chatwith_uses_normal_click_after_tray_restore() -> None:
    windows = TrayRestoredWindowController()
    keyboard = FakeKeyboard()
    wx = WeChat(
        window_controller=windows,
        keyboard=keyboard,
        search_options=SearchOptions(result_wait=0, search_box_offset=(120, 55), window_ready_wait=0),
    )

    result = wx.ChatWith("restored")

    assert result
    assert keyboard.actions[:5] == [
        ("hotkey", ("ctrl", "f")),
        ("select_all", None),
        ("paste", ("restored", True)),
        ("press", "enter"),
    ]


def test_sendmsg_opens_chat_pastes_message_and_presses_enter(tmp_path) -> None:
    windows = FakeWindowController()
    keyboard = FakeKeyboard()
    wx = WeChat(
        window_controller=windows,
        keyboard=keyboard,
        search_options=SearchOptions(result_wait=0, chat_open_wait=0),
        bridge_store_path=tmp_path / "bridge.sqlite3",
    )

    result = wx.SendMsg(" 你好，今天开会吗？ ", " 张三 ")

    assert result
    assert windows.activated
    assert result.data["who"] == "张三"
    assert result.data["message"] == "你好，今天开会吗？"
    assert result.data["message_length"] == len("你好，今天开会吗？")
    assert keyboard.actions == [
        ("hotkey", ("ctrl", "f")),
        ("select_all", None),
        ("paste", ("张三", True)),
        ("press", "enter"),
        ("paste", ("你好，今天开会吗？", True)),
        ("press", "enter"),
    ]


def test_send_message_alias_uses_who_then_message(tmp_path) -> None:
    keyboard = FakeKeyboard()
    wx = WeChat(
        window_controller=FakeWindowController(),
        keyboard=keyboard,
        search_options=SearchOptions(result_wait=0, chat_open_wait=0),
        bridge_store_path=tmp_path / "bridge.sqlite3",
    )

    result = wx.send_message("文件传输助手", "测试消息")

    assert result
    assert result.data["who"] == "文件传输助手"
    assert result.data["message"] == "测试消息"


def test_sendmsg_records_outgoing_echo_in_custom_bridge_store(tmp_path) -> None:
    store_path = tmp_path / "bridge.sqlite3"
    wx = WeChat(
        window_controller=FakeWindowController(),
        keyboard=FakeKeyboard(),
        search_options=SearchOptions(result_wait=0, chat_open_wait=0),
        bridge_store_path=store_path,
    )

    result = wx.SendMsg(" hello ", " alice ")

    assert result
    assert BridgeStore(store_path).matches_outgoing_echo("alice", "hello")


def test_send_message_alias_records_outgoing_echo(tmp_path) -> None:
    store_path = tmp_path / "bridge.sqlite3"
    wx = WeChat(
        window_controller=FakeWindowController(),
        keyboard=FakeKeyboard(),
        search_options=SearchOptions(result_wait=0, chat_open_wait=0),
        bridge_store_path=store_path,
    )

    result = wx.send_message("alice", "hello")

    assert result
    assert BridgeStore(store_path).matches_outgoing_echo("alice", "hello")


def test_sendmsg_rejects_empty_message() -> None:
    wx = WeChat(window_controller=FakeWindowController(), keyboard=FakeKeyboard())

    result = wx.SendMsg("   ", "张三")

    assert not result
    assert result.status == "failure"


def test_wxauto4_backend_starts_watchdog_before_construct() -> None:
    events: list[object] = []

    class FakeWxauto4WeChat:
        def __init__(self, **kwargs: object) -> None:
            events.append(("construct", kwargs))

    backend = Wxauto4Backend(before_construct=lambda: events.append("watchdog"))
    backend._wechat_class = lambda: FakeWxauto4WeChat

    result = backend.prepare_window()

    assert result
    assert events == [
        "watchdog",
        ("construct", {"ads": False, "resize": False}),
    ]
