from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from my_wxauto import BridgeMessage, ConversationBatch
from my_wxauto import probes
from my_wxauto import cli
from my_wxauto.listener import ListenerStats
from my_wxauto.response import WxResponse


def test_root_compat_package_exports_bridge_types_without_pythonpath() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from my_wxauto import BridgeMessage, ConversationBatch; "
            "print(BridgeMessage.__name__, ConversationBatch.__name__)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "BridgeMessage ConversationBatch\n"


class FakeWeChat:
    instances: list["FakeWeChat"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.listen_kwargs: dict[str, object] = {}
        FakeWeChat.instances.append(self)

    def ChatWith(self, who: str) -> WxResponse:
        self.calls.append(("ChatWith", (who,)))
        return WxResponse.success("opened", {"who": who})

    def SendMsg(self, msg: str, who: str) -> WxResponse:
        self.calls.append(("SendMsg", (msg, who)))
        return WxResponse.success("sent", {"who": who, "message": msg})

    def listen_conversation_batches(self, callback, **kwargs: object) -> ListenerStats:
        self.calls.append(("listen_conversation_batches", (callback,)))
        self.listen_kwargs = kwargs
        message = BridgeMessage(
            chat_name="group",
            content="hello",
            message_type="text",
            sender="alice",
            is_self=False,
            time_text="15:41",
            occurrence_index=2,
        ).with_key()
        callback(
            ConversationBatch(
                batch_id="batch-1",
                chat_name="group",
                messages=(message,),
                created_at=10.0,
                frozen_at=12.0,
                status="frozen",
            )
        )
        return ListenerStats(
            flash_count=1,
            event_count=1,
            duration_seconds=2.5,
            stopped_reason="max_events",
        )


def test_main_runs_wakeup_probe(monkeypatch, capsys) -> None:
    calls: list[dict[str, object]] = []

    def fake_watch_unread_wakeup(**kwargs: object) -> None:
        calls.append(kwargs)
        print("wakeup probe")

    monkeypatch.setattr(probes, "watch_unread_wakeup", fake_watch_unread_wakeup)

    exit_code = cli.main(
        [
            "--watch-wakeup",
            "10",
            "--probe-interval",
            "0.2",
            "--probe-max-controls",
            "12",
            "--wakeup-burst-changes",
            "3",
            "--wakeup-burst-window",
            "2",
            "--wakeup-cooldown",
            "4",
            "--wakeup-action-timeout",
            "9",
            "--wakeup-max-probes",
            "2",
            "--wakeup-open-unread",
        ]
    )

    assert exit_code == 0
    assert calls == [
        {
            "seconds": 10.0,
            "interval": 0.2,
            "max_controls": 12,
            "min_changes": 3,
            "window_seconds": 2.0,
            "cooldown_seconds": 4.0,
            "action_timeout": 9.0,
            "max_probes": 2,
            "open_unread_messages": True,
        }
    ]
    assert capsys.readouterr().out == "wakeup probe\n"


def test_main_opens_chat_without_message(monkeypatch, capsys) -> None:
    FakeWeChat.instances.clear()
    monkeypatch.setattr(cli, "WeChat", FakeWeChat)

    exit_code = cli.main(["张三"])

    assert exit_code == 0
    assert FakeWeChat.instances[0].calls == [("ChatWith", ("张三",))]
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert output["data"]["who"] == "张三"


def test_main_passes_search_down_options(monkeypatch, capsys) -> None:
    FakeWeChat.instances.clear()
    monkeypatch.setattr(cli, "WeChat", FakeWeChat)

    exit_code = cli.main(
        [
            "张三",
            "--search-down-count",
            "2",
            "--search-down-interval",
            "0.1",
        ]
    )

    options = FakeWeChat.instances[0].kwargs["search_options"]
    assert exit_code == 0
    assert options.search_down_count == 2
    assert options.search_down_interval == 0.1
    capsys.readouterr()


def test_main_sends_message_when_message_argument_is_present(monkeypatch, capsys) -> None:
    FakeWeChat.instances.clear()
    monkeypatch.setattr(cli, "WeChat", FakeWeChat)

    exit_code = cli.main(["张三", "--message", "你好"])

    assert exit_code == 0
    assert FakeWeChat.instances[0].calls == [("SendMsg", ("你好", "张三"))]
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "success"
    assert output["data"]["message"] == "你好"


def test_main_writes_utf8_output_file(monkeypatch, capsys, tmp_path) -> None:
    FakeWeChat.instances.clear()
    monkeypatch.setattr(cli, "WeChat", FakeWeChat)
    output_path = tmp_path / "probe-output.txt"

    exit_code = cli.main(["张三", "--output", str(output_path)])

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["status"] == "success"
    assert output["data"]["who"] == "张三"


def test_main_listens_conversation_batches_with_debug_fields(monkeypatch, capsys, tmp_path) -> None:
    FakeWeChat.instances.clear()
    monkeypatch.setattr(cli, "WeChat", FakeWeChat)
    store_path = tmp_path / "debug.sqlite3"

    exit_code = cli.main(
        [
            "--listen-batches",
            "--listen-seconds",
            "30",
            "--listen-max-events",
            "1",
            "--listen-max-probes",
            "2",
            "--listen-max-chats",
            "3",
            "--listen-resolve-senders",
            "profile_card",
            "--listen-sender-limit",
            "4",
            "--store-path",
            str(store_path),
        ]
    )

    output = capsys.readouterr().out
    wx = FakeWeChat.instances[0]
    message_key = BridgeMessage(
        chat_name="group",
        content="hello",
        message_type="text",
        sender="alice",
        is_self=False,
        time_text="15:41",
        occurrence_index=2,
    ).with_key().message_key

    assert exit_code == 0
    assert wx.kwargs["bridge_store_path"] == str(store_path)
    assert wx.calls[0][0] == "listen_conversation_batches"
    assert wx.listen_kwargs == {
        "seconds": 30.0,
        "max_events": 1,
        "max_probes": 2,
        "max_chats_per_drain": 3,
        "store_path": str(store_path),
        "resolve_senders": "profile_card",
        "sender_resolve_limit": 4,
    }
    assert "chat: group" in output
    assert "message_count: 1" in output
    assert f"key={message_key[:12]}" in output
    assert "index=2" in output
    assert "time=15:41" in output
    assert "sender=alice" in output
    assert "is_self=False" in output
    assert "type=text" in output
    assert "content=hello" in output
    assert "stopped_reason=max_events" in output


def test_main_starts_bridge_server(monkeypatch, tmp_path) -> None:
    calls: list[object] = []
    store_path = tmp_path / "bridge.sqlite3"

    def fake_run_bridge_server(config: object) -> None:
        calls.append(config)

    monkeypatch.setattr(cli, "run_bridge_server", fake_run_bridge_server)

    exit_code = cli.main(
        [
            "--bridge-server",
            "--bridge-host",
            "0.0.0.0",
            "--bridge-port",
            "9876",
            "--bridge-queue-size",
            "7",
            "--store-path",
            str(store_path),
            "--listen-max-chats",
            "3",
            "--listen-resolve-senders",
            "profile_card",
            "--listen-sender-limit",
            "4",
            "--no-wxauto4",
            "--debug",
            "--trace-ui",
        ]
    )

    assert exit_code == 0
    assert len(calls) == 1
    config = calls[0]
    assert config.host == "0.0.0.0"
    assert config.port == 9876
    assert config.queue_size == 7
    assert config.store_path == str(store_path)
    assert config.max_chats_per_drain == 3
    assert config.resolve_senders == "profile_card"
    assert config.sender_resolve_limit == 4
    assert config.prefer_wxauto4 is False
    assert config.debug is True
    assert config.trace_ui is True
