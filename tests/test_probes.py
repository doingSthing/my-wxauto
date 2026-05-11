from __future__ import annotations

import json

import pytest

from my_wxauto.probes import (
    TaskbarFlashDetector,
    _find_red_components,
    _parse_session_items,
    _parse_chat_message_items,
    _pick_pixel_restore_click_point,
    _pick_tray_badge_click_point,
    _session_click_point,
)
from my_wxauto import probes
from my_wxauto.window import WeChatWindow, WindowRect


def test_taskbar_flash_detector_requires_burst_changes() -> None:
    detector = TaskbarFlashDetector(min_changes=4, window_seconds=3.0, cooldown_seconds=5.0)

    assert detector.observe(0.0, ("baseline",)) is None
    assert detector.observe(0.5, ("changed-once",)) is None
    assert detector.observe(4.0, ("changed-after-window",)) is None
    assert detector.observe(4.5, ("changed-again",)) is None

    assert detector.observe(4.8, ("changed-third-time-in-window",)) is None
    event = detector.observe(5.0, ("changed-fourth-time-in-window",))

    assert event is not None
    assert event["change_count"] == 4
    assert event["window_seconds"] == 3.0


def test_taskbar_flash_detector_cooldown_suppresses_repeated_events() -> None:
    detector = TaskbarFlashDetector(min_changes=3, window_seconds=2.0, cooldown_seconds=5.0)

    assert detector.observe(0.0, ("baseline",)) is None
    assert detector.observe(0.2, ("a",)) is None
    assert detector.observe(0.4, ("b",)) is None
    first = detector.observe(0.6, ("c",))
    assert first is not None

    assert detector.observe(0.8, ("d",)) is None
    assert detector.observe(1.0, ("e",)) is None
    assert detector.observe(1.2, ("f",)) is None

    assert detector.observe(5.7, ("g",)) is None
    assert detector.observe(5.9, ("h",)) is None
    second = detector.observe(6.1, ("i",))
    assert second is not None


def test_watch_unread_wakeup_times_out_probe_without_hanging(monkeypatch, capsys) -> None:
    icons = [
        [{"source": "taskbar", "class_name": "Shell_TrayWnd", "rectangle": [0, 0, 10, 10], "image_sha1": "a"}],
        [{"source": "taskbar", "class_name": "Shell_TrayWnd", "rectangle": [0, 0, 10, 10], "image_sha1": "b"}],
    ]
    calls = {"count": 0}

    def fake_icons() -> list[dict[str, object]]:
        calls["count"] += 1
        return icons[min(calls["count"] - 1, 1)]

    def fake_probe(
        *,
        max_controls: int,
        timeout: float,
        restore_icons,
        open_unread_messages: bool,
        on_progress,
    ) -> dict[str, object]:
        assert open_unread_messages is False
        on_progress({"stage": "collect_uia.before"})
        return {"status": "timeout", "max_controls": max_controls, "timeout": timeout}

    monkeypatch.setattr(probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)

    probes.watch_unread_wakeup(
        seconds=0.03,
        interval=0.01,
        min_changes=1,
        max_controls=12,
        action_timeout=2.5,
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    payloads = [json.loads(line.removeprefix("MY_WXAUTO_WAKEUP ")) for line in lines]

    assert any(payload["status"] == "probe_started" for payload in payloads)
    assert any(
        payload["status"] == "probe_progress" and payload["stage"] == "collect_uia.before"
        for payload in payloads
    )
    assert any(
        payload["status"] == "probe_timeout"
        and payload["max_controls"] == 12
        and payload["timeout"] == 2.5
        for payload in payloads
    )
    assert payloads[-1]["status"] == "finished"


def test_watch_unread_wakeup_stops_after_default_first_probe(monkeypatch, capsys) -> None:
    hashes = iter(["a", "b", "c", "d"])

    def fake_icons() -> list[dict[str, object]]:
        image_hash = next(hashes, "z")
        return [{"source": "taskbar", "class_name": "Shell_TrayWnd", "rectangle": [0, 0, 10, 10], "image_sha1": image_hash}]

    def fake_probe(
        *,
        max_controls: int,
        timeout: float,
        restore_icons,
        open_unread_messages: bool,
        on_progress,
    ) -> dict[str, object]:
        assert open_unread_messages is False
        return {"status": "ok", "sessions": [], "unread_sessions": [], "unread_count": 0}

    monkeypatch.setattr(probes, "_ensure_windows", lambda: None)
    monkeypatch.setattr(probes, "inspect_wechat_taskbar_icons", fake_icons)
    monkeypatch.setattr(probes, "_probe_sessions_after_wakeup_with_timeout", fake_probe)

    probes.watch_unread_wakeup(seconds=5, interval=0.01, min_changes=1)

    payloads = [
        json.loads(line.removeprefix("MY_WXAUTO_WAKEUP "))
        for line in capsys.readouterr().out.splitlines()
        if line
    ]

    assert [payload["status"] for payload in payloads].count("sessions") == 1
    assert payloads[-1] == {"status": "finished", "flash_count": 1}


def test_probe_sessions_after_wakeup_uses_direct_win32_restore(monkeypatch) -> None:
    constructed_kwargs: list[dict[str, object]] = []
    window = WeChatWindow(
        hwnd=100,
        title="WeChat",
        class_name="Qt51514QWindowIcon",
        pid=42,
        process_name="Weixin.exe",
        exe=r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        rect=WindowRect(-32000, -32000, -31840, -31972),
        visible=True,
        minimized=True,
    )
    restored = WeChatWindow(
        hwnd=100,
        title="WeChat",
        class_name="Qt51514QWindowIcon",
        pid=42,
        process_name="Weixin.exe",
        exe=r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        rect=WindowRect(10, 20, 900, 700),
        visible=True,
        minimized=False,
    )

    class FakeController:
        def __init__(self, **kwargs: object) -> None:
            constructed_kwargs.append(kwargs)

        def find_main_window(self, reveal: bool = True) -> WeChatWindow:
            assert reveal is True
            return window

        def activate(self, target: WeChatWindow, wait: float = 0.35) -> WeChatWindow:
            assert target is window
            assert wait == 0.35
            return restored

        def wait_until_ready(
            self,
            target: WeChatWindow,
            *,
            timeout: float,
            stable_for: float,
        ) -> WeChatWindow:
            assert target is restored
            assert timeout == 5.0
            assert stable_for == 0.3
            return restored

    monkeypatch.setattr(probes, "WeChatWindowController", FakeController)
    monkeypatch.setattr(probes, "_collect_uia_controls", lambda *args, **kwargs: [])

    result = probes._probe_sessions_after_wakeup(max_controls=12)

    assert constructed_kwargs == [{"prefer_tray_restore": False}]
    assert result["window"]["minimized"] is False
    assert result["sessions"] == []


def test_pick_tray_badge_click_point_prefers_right_side_taskbar_badge() -> None:
    icons = [
        {
            "source": "primary-taskbar",
            "rectangle": [0, 1032, 1920, 1080],
            "red_badges": [
                {"rect": {"left": 331, "top": 1044, "right": 351, "bottom": 1057}, "center": [341, 1050]},
                {"rect": {"left": 1605, "top": 1051, "right": 1614, "bottom": 1061}, "center": [1609, 1056]},
                {"rect": {"left": 1615, "top": 1051, "right": 1621, "bottom": 1056}, "center": [1618, 1053]},
            ],
        }
    ]

    assert _pick_tray_badge_click_point(icons) == (1613, 1056)


def test_pick_tray_badge_click_point_falls_back_to_single_taskbar_badge() -> None:
    icons = [
        {
            "source": "primary-taskbar",
            "rectangle": [0, 1032, 1920, 1080],
            "red_badges": [
                {"rect": {"left": 331, "top": 1044, "right": 351, "bottom": 1057}, "center": [341, 1050]},
            ],
        }
    ]

    assert _pick_tray_badge_click_point(icons) == (341, 1050)


def test_pick_pixel_restore_click_point_prefers_wechat_green_tray_icon() -> None:
    icons = [
        {
            "source": "primary-taskbar",
            "rectangle": [0, 1032, 1920, 1080],
            "green_icons": [
                {"rect": {"left": 1637, "top": 1048, "right": 1653, "bottom": 1064}, "center": [1645, 1056], "area": 179},
                {"rect": {"left": 1702, "top": 1052, "right": 1717, "bottom": 1064}, "center": [1710, 1058], "area": 94},
            ],
            "red_badges": [
                {"rect": {"left": 1605, "top": 1051, "right": 1621, "bottom": 1061}, "center": [1613, 1056]},
            ],
        }
    ]

    assert _pick_pixel_restore_click_point(icons) == (1645, 1056)


def test_probe_sessions_after_wakeup_uses_toggle_hotkey_for_hidden_window(monkeypatch) -> None:
    constructed_kwargs: list[dict[str, object]] = []
    events: list[str] = []
    hidden = WeChatWindow(
        hwnd=100,
        title="WeChat",
        class_name="Qt51514QWindowIcon",
        pid=42,
        process_name="Weixin.exe",
        exe=r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        rect=WindowRect(1000, 20, 1900, 900),
        visible=False,
        minimized=False,
    )
    restored = WeChatWindow(
        hwnd=100,
        title="WeChat",
        class_name="Qt51514QWindowIcon",
        pid=42,
        process_name="Weixin.exe",
        exe=r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        rect=WindowRect(1000, 20, 1900, 900),
        visible=True,
        minimized=False,
    )

    class FakeController:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            constructed_kwargs.append(kwargs)

        def find_main_window(self, reveal: bool = True) -> WeChatWindow:
            return hidden

        def get_window(self, hwnd: int) -> WeChatWindow | None:
            assert hwnd == hidden.hwnd
            return restored

        def activate(self, target: WeChatWindow, wait: float = 0.35) -> WeChatWindow:
            assert target is restored
            events.append("activate")
            return restored

        def wait_until_ready(
            self,
            target: WeChatWindow,
            *,
            timeout: float,
            stable_for: float,
        ) -> WeChatWindow:
            return restored

    monkeypatch.setattr(probes, "WeChatWindowController", FakeController)
    monkeypatch.setattr(probes, "_collect_uia_controls", lambda *args, **kwargs: [])
    monkeypatch.setattr(probes, "_send_wechat_toggle_hotkey", lambda: events.append("hotkey"))

    result = probes._probe_sessions_after_wakeup(
        max_controls=12,
        restore_icons=[
            {
                "source": "primary-taskbar",
                "rectangle": [0, 1032, 1920, 1080],
                "green_icons": [
                    {"rect": {"left": 1637, "top": 1048, "right": 1653, "bottom": 1064}, "center": [1645, 1056], "area": 179}
                ],
                "red_badges": [
                    {"rect": {"left": 1605, "top": 1051, "right": 1614, "bottom": 1061}, "center": [1609, 1056]}
                ],
            }
        ],
    )

    assert constructed_kwargs == [{"prefer_tray_restore": False}]
    assert events == ["hotkey", "activate"]
    assert result["window"]["visible"] is True


def test_session_click_point_uses_session_rect_center() -> None:
    session = {"rect": {"left": 10, "top": 20, "right": 110, "bottom": 80}}

    assert _session_click_point(session) == (60, 50)


def test_parse_chat_message_items_skips_time_rows_and_empty_items() -> None:
    controls = [
        {
            "name": "09:25",
            "control_type": "ListItem",
            "class_name": "mmui::ChatItemView",
            "automation_id": "",
            "rect": {"left": 1, "top": 2, "right": 3, "bottom": 4},
        },
        {
            "name": "hello",
            "control_type": "ListItem",
            "class_name": "mmui::ChatTextItemView",
            "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
            "rect": {"left": 10, "top": 20, "right": 30, "bottom": 40},
        },
        {
            "name": "",
            "control_type": "ListItem",
            "class_name": "mmui::ChatTextItemView",
            "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
            "rect": {},
        },
    ]

    messages = _parse_chat_message_items(controls)

    assert messages == [
        {
            "content": "hello",
            "message_type": "text",
            "sender": None,
            "time_text": "09:25",
            "raw_name": "hello",
            "class_name": "mmui::ChatTextItemView",
            "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
            "rect": {"left": 10, "top": 20, "right": 30, "bottom": 40},
        }
    ]


def test_parse_chat_message_items_skips_left_session_cells() -> None:
    controls = [
        {
            "name": "张勋\npreview\n10:02",
            "control_type": "ListItem",
            "class_name": "mmui::ChatSessionCell",
            "automation_id": "session_item_张勋",
            "rect": {"left": 301, "top": 286, "right": 541, "bottom": 350},
        },
        {
            "name": "actual message",
            "control_type": "ListItem",
            "class_name": "mmui::ChatTextItemView",
            "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
            "rect": {"left": 542, "top": 591, "right": 1118, "bottom": 647},
        },
    ]

    messages = _parse_chat_message_items(controls)

    assert [message["content"] for message in messages] == ["actual message"]


def test_parse_chat_message_items_assigns_visible_time_to_following_messages() -> None:
    controls = [
        {
            "name": "before time",
            "control_type": "ListItem",
            "class_name": "mmui::ChatTextItemView",
            "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
            "rect": {"left": 10, "top": 20, "right": 30, "bottom": 40},
        },
        {
            "name": "15:40",
            "control_type": "ListItem",
            "class_name": "mmui::ChatItemView",
            "automation_id": "",
            "rect": {"left": 10, "top": 41, "right": 30, "bottom": 60},
        },
        {
            "name": "after time",
            "control_type": "ListItem",
            "class_name": "mmui::ChatTextItemView",
            "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
            "rect": {"left": 10, "top": 61, "right": 30, "bottom": 80},
        },
    ]

    messages = _parse_chat_message_items(controls)

    assert messages[0]["content"] == "before time"
    assert messages[0]["time_text"] is None
    assert messages[1]["content"] == "after time"
    assert messages[1]["time_text"] == "15:40"


def test_probe_sessions_after_wakeup_can_open_unread_session_and_collect_messages(monkeypatch) -> None:
    window = WeChatWindow(
        hwnd=100,
        title="WeChat",
        class_name="Qt51514QWindowIcon",
        pid=42,
        process_name="Weixin.exe",
        exe=r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        rect=WindowRect(100, 200, 1000, 900),
        visible=True,
        minimized=False,
    )
    clicks: list[tuple[int, int]] = []
    collect_calls = {"count": 0}

    class FakeController:
        def __init__(self, **kwargs: object) -> None:
            pass

        def find_main_window(self, reveal: bool = True) -> WeChatWindow:
            return window

        def activate(self, target: WeChatWindow, wait: float = 0.35) -> WeChatWindow:
            return target

        def wait_until_ready(
            self,
            target: WeChatWindow,
            *,
            timeout: float,
            stable_for: float,
        ) -> WeChatWindow:
            return target

    def fake_collect(*args: object, **kwargs: object) -> list[dict[str, object]]:
        collect_calls["count"] += 1
        if collect_calls["count"] == 1:
            return [
                {
                    "name": "张勋\n[1条] \nhello\n09:38\n",
                    "class_name": "mmui::ChatSessionCell",
                    "automation_id": "session_item_张勋",
                    "rect": {"left": 300, "top": 286, "right": 540, "bottom": 350},
                }
            ]
        return [
            {
                "name": "hello",
                "control_type": "ListItem",
                "class_name": "mmui::ChatTextItemView",
                "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
                "rect": {"left": 542, "top": 591, "right": 1118, "bottom": 647},
            }
        ]

    monkeypatch.setattr(probes, "WeChatWindowController", FakeController)
    monkeypatch.setattr(probes, "_collect_uia_controls", fake_collect)
    monkeypatch.setattr(probes, "_click_point", lambda point: clicks.append(point))
    monkeypatch.setattr(probes.time, "sleep", lambda _seconds: None)

    result = probes._probe_sessions_after_wakeup(max_controls=12, open_unread_messages=True)

    assert clicks == [(420, 318)]
    assert result["unread_count"] == 1
    assert result["opened_unread_chats"][0]["chat_name"] == "张勋"
    assert result["opened_unread_chats"][0]["messages"][0]["content"] == "hello"


def test_probe_sessions_after_wakeup_collects_top_session_when_unread_marker_is_cleared(monkeypatch) -> None:
    window = WeChatWindow(
        hwnd=100,
        title="WeChat",
        class_name="Qt51514QWindowIcon",
        pid=42,
        process_name="Weixin.exe",
        exe=r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        rect=WindowRect(100, 200, 1000, 900),
        visible=True,
        minimized=False,
    )
    clicks: list[tuple[int, int]] = []
    collect_calls = {"count": 0}

    class FakeController:
        def __init__(self, **kwargs: object) -> None:
            pass

        def find_main_window(self, reveal: bool = True) -> WeChatWindow:
            return window

        def activate(self, target: WeChatWindow, wait: float = 0.35) -> WeChatWindow:
            return target

        def wait_until_ready(
            self,
            target: WeChatWindow,
            *,
            timeout: float,
            stable_for: float,
        ) -> WeChatWindow:
            return target

    def fake_collect(*args: object, **kwargs: object) -> list[dict[str, object]]:
        collect_calls["count"] += 1
        if collect_calls["count"] == 1:
            return [
                {
                    "name": "寮犲媼\nwake message\n09:58\n",
                    "class_name": "mmui::ChatSessionCell",
                    "automation_id": "session_item_寮犲媼",
                    "rect": {"left": 300, "top": 286, "right": 540, "bottom": 350},
                }
            ]
        return [
            {
                "name": "wake message",
                "control_type": "ListItem",
                "class_name": "mmui::ChatTextItemView",
                "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
                "rect": {"left": 542, "top": 591, "right": 1118, "bottom": 647},
            }
        ]

    monkeypatch.setattr(probes, "WeChatWindowController", FakeController)
    monkeypatch.setattr(probes, "_collect_uia_controls", fake_collect)
    monkeypatch.setattr(probes, "_click_point", lambda point: clicks.append(point))
    monkeypatch.setattr(probes.time, "sleep", lambda _seconds: None)

    result = probes._probe_sessions_after_wakeup(max_controls=12, open_unread_messages=True)

    assert result["unread_count"] == 0
    assert clicks == [(420, 318)]
    assert result["opened_unread_chats"][0]["chat_name"] == "寮犲媼"
    assert result["opened_unread_chats"][0]["source"] == "top_session_after_flash"
    assert result["opened_unread_chats"][0]["messages"][0]["content"] == "wake message"


def test_open_unread_sessions_respects_max_unread_chats_and_reports_each_chat(monkeypatch) -> None:
    window = WeChatWindow(
        hwnd=100,
        title="WeChat",
        class_name="Qt51514QWindowIcon",
        pid=42,
        process_name="Weixin.exe",
        exe=r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        rect=WindowRect(100, 200, 1000, 900),
        visible=True,
        minimized=False,
    )
    sessions = [
        {"chat_name": "a", "unread_count": 2, "rect": {"left": 300, "top": 280, "right": 540, "bottom": 340}},
        {"chat_name": "b", "rect": {"left": 300, "top": 340, "right": 540, "bottom": 400}},
        {"chat_name": "c", "rect": {"left": 300, "top": 400, "right": 540, "bottom": 460}},
    ]
    clicked: list[tuple[int, int]] = []
    reported: list[dict[str, object]] = []

    def fake_collect(window_arg, *, region, max_controls):
        chat_name = sessions[len(clicked) - 1]["chat_name"]
        return [
            {
                "name": f"message from {chat_name}",
                "control_type": "ListItem",
                "class_name": "mmui::ChatTextItemView",
                "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
                "rect": {"left": 542, "top": 591, "right": 1118, "bottom": 647},
            }
        ]

    monkeypatch.setattr(probes, "_click_point", lambda point: clicked.append(point))
    monkeypatch.setattr(probes, "_collect_uia_controls", fake_collect)
    monkeypatch.setattr(probes.time, "sleep", lambda _seconds: None)

    opened = probes._open_unread_sessions_and_collect_messages(
        window,
        sessions,
        max_controls=12,
        max_unread_chats=2,
        on_chat_opened=reported.append,
    )

    assert [chat["chat_name"] for chat in opened] == ["a", "b"]
    assert [chat["chat_name"] for chat in reported] == ["a", "b"]
    assert opened[0]["unread_count"] == 2
    assert reported[0]["unread_count"] == 2
    assert len(clicked) == 2


def test_open_unread_sessions_stops_at_ui_busy_budget(monkeypatch) -> None:
    window = WeChatWindow(
        hwnd=100,
        title="WeChat",
        class_name="Qt51514QWindowIcon",
        pid=42,
        process_name="Weixin.exe",
        exe=r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        rect=WindowRect(100, 200, 1000, 900),
        visible=True,
        minimized=False,
    )
    sessions = [
        {"chat_name": "a", "rect": {"left": 300, "top": 280, "right": 540, "bottom": 340}},
        {"chat_name": "b", "rect": {"left": 300, "top": 340, "right": 540, "bottom": 400}},
    ]
    times = iter([100.0, 100.0, 116.0, 116.0])

    monkeypatch.setattr(probes.time, "perf_counter", lambda: next(times, 116.0))
    monkeypatch.setattr(probes.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(probes, "_click_point", lambda _point: None)
    monkeypatch.setattr(
        probes,
        "_collect_uia_controls",
        lambda *_args, **_kwargs: [
            {
                "name": "hello",
                "control_type": "ListItem",
                "class_name": "mmui::ChatTextItemView",
                "automation_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
                "rect": {"left": 542, "top": 591, "right": 1118, "bottom": 647},
            }
        ],
    )

    opened = probes._open_unread_sessions_and_collect_messages(
        window,
        sessions,
        max_controls=12,
        max_unread_chats=5,
        max_ui_busy_seconds=15.0,
    )

    assert [chat["chat_name"] for chat in opened] == ["a"]


def test_open_unread_sessions_reports_no_click_point_chat(monkeypatch) -> None:
    window = WeChatWindow(
        hwnd=100,
        title="WeChat",
        class_name="Qt51514QWindowIcon",
        pid=42,
        process_name="Weixin.exe",
        exe=r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        rect=WindowRect(100, 200, 1000, 900),
        visible=True,
        minimized=False,
    )
    reported: list[dict[str, object]] = []
    progress: list[tuple[str, dict[str, object] | None]] = []

    opened = probes._open_unread_sessions_and_collect_messages(
        window,
        [{"chat_name": "a"}],
        max_controls=12,
        on_chat_opened=reported.append,
        report_progress=lambda stage, extra=None: progress.append((stage, extra)),
    )

    assert len(opened) == 1
    assert opened[0]["status"] == "no_click_point"
    assert reported == [opened[0]]
    assert [stage for stage, _extra in progress].count("open_unread.chat") == 1
    assert [extra for stage, extra in progress if stage == "open_unread.chat"] == [{"chat": opened[0]}]


def test_probe_sessions_after_wakeup_with_timeout_dispatches_parent_chat_callback(monkeypatch) -> None:
    messages = [
        {"status": "progress", "stage": "open_unread.chat", "chat": {"chat_name": "a"}},
        {"status": "ok", "opened_unread_chats": []},
    ]
    captured: dict[str, object] = {}
    opened: list[dict[str, object]] = []

    class FakeQueue:
        def __init__(self, maxsize: int) -> None:
            captured["queue_maxsize"] = maxsize

        def get_nowait(self) -> dict[str, object]:
            if not messages:
                raise probes.queue.Empty
            return messages.pop(0)

    class FakeProcess:
        daemon = False
        exitcode = 0

        def __init__(self, *, target, args) -> None:
            captured["target"] = target
            captured["args"] = args

        def start(self) -> None:
            captured["started"] = True

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            captured["join_timeout"] = timeout

    class FakeContext:
        def Queue(self, maxsize: int) -> FakeQueue:
            return FakeQueue(maxsize)

        def Process(self, *, target, args) -> FakeProcess:
            return FakeProcess(target=target, args=args)

    monkeypatch.setattr(probes.mp, "get_context", lambda method: FakeContext())
    callback = lambda chat: opened.append(chat)

    result = probes._probe_sessions_after_wakeup_with_timeout(
        max_controls=12,
        timeout=5.0,
        restore_icons=[{"source": "taskbar"}],
        open_unread_messages=True,
        max_unread_chats=3,
        max_ui_busy_seconds=9.5,
        on_chat_opened=callback,
    )

    args = captured["args"]
    assert result["status"] == "ok"
    assert [chat["chat_name"] for chat in opened] == ["a"]
    assert len(args) == 6
    assert args[1:] == (12, [{"source": "taskbar"}], True, 3, 9.5)
    assert callback not in args


def test_probe_sessions_after_wakeup_with_timeout_cleans_worker_when_parent_callback_fails(monkeypatch) -> None:
    messages = [
        {"status": "progress", "stage": "open_unread.chat", "chat": {"chat_name": "a"}},
    ]
    captured: dict[str, object] = {}

    class FakeQueue:
        def __init__(self, maxsize: int) -> None:
            captured["queue_maxsize"] = maxsize

        def get_nowait(self) -> dict[str, object]:
            if not messages:
                raise probes.queue.Empty
            return messages.pop(0)

    class FakeProcess:
        daemon = False
        exitcode = None

        def __init__(self, *, target, args) -> None:
            captured["target"] = target
            captured["args"] = args
            self.alive = True

        def start(self) -> None:
            captured["started"] = True

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            captured["terminated"] = True
            self.alive = False

        def join(self, timeout: float | None = None) -> None:
            captured.setdefault("join_timeouts", []).append(timeout)

    class FakeContext:
        def Queue(self, maxsize: int) -> FakeQueue:
            return FakeQueue(maxsize)

        def Process(self, *, target, args) -> FakeProcess:
            return FakeProcess(target=target, args=args)

    def callback(_chat: dict[str, object]) -> None:
        raise RuntimeError("callback failed")

    monkeypatch.setattr(probes.mp, "get_context", lambda method: FakeContext())

    with pytest.raises(RuntimeError, match="callback failed"):
        probes._probe_sessions_after_wakeup_with_timeout(
            max_controls=12,
            timeout=5.0,
            on_chat_opened=callback,
        )

    assert captured["terminated"] is True
    assert captured["join_timeouts"] == [1.0]


def test_find_red_components_returns_badge_candidates() -> None:
    width = 20
    height = 16
    pixels = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 4
            pixels[i : i + 4] = bytes((240, 240, 240, 0))

    for y in range(3, 8):
        for x in range(5, 10):
            i = (y * width + x) * 4
            pixels[i : i + 4] = bytes((40, 40, 230, 0))

    badges = _find_red_components(bytes(pixels), width, height, origin=(100, 200))

    assert len(badges) == 1
    assert badges[0].rect == {
        "left": 105,
        "top": 203,
        "right": 110,
        "bottom": 208,
        "width": 5,
        "height": 5,
    }
    assert badges[0].area == 25


def test_parse_session_items_extracts_unread_count() -> None:
    controls = [
        {
            "name": "张勋\n1\n11:31\n",
            "class_name": "mmui::ChatSessionCell",
            "automation_id": "session_item_张勋",
            "rect": {"left": 1, "top": 2, "right": 3, "bottom": 4},
        },
        {
            "name": "张勋\n[1条] \n55\n14:29\n",
            "class_name": "mmui::ChatSessionCell",
            "automation_id": "session_item_张勋",
            "rect": {},
        },
        {
            "name": "文件传输助手\n你好\n昨天 16:57\n",
            "class_name": "mmui::ChatSessionCell",
            "automation_id": "session_item_文件传输助手",
            "rect": {},
        },
    ]

    sessions = _parse_session_items(controls)

    assert sessions[0]["chat_name"] == "张勋"
    assert sessions[0]["unread_count"] == 0
    assert sessions[0]["preview"] == "1"
    assert sessions[0]["time"] == "11:31"
    assert sessions[1]["chat_name"] == "张勋"
    assert sessions[1]["unread_count"] == 1
    assert sessions[1]["has_unread"] is True
    assert sessions[1]["unread_marker"] == "[1条]"
    assert sessions[1]["preview"] == "55"
    assert sessions[1]["time"] == "14:29"
    assert sessions[2]["chat_name"] == "文件传输助手"
    assert sessions[2]["unread_count"] == 0
    assert sessions[2]["preview"] == "你好"
