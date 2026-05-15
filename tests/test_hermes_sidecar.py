from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from my_wxauto import hermes_sidecar
from my_wxauto.hermes_sidecar import BridgeClient, format_prompt, session_name_for_chat


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class _FakeBridge:
    def __init__(self, health_payload: dict[str, object] | None = None) -> None:
        self.health_payload = health_payload or {"status": "ok"}
        self.sent: list[tuple[str, str]] = []
        self.acked: list[str] = []
        self.completed: list[str] = []

    def health(self) -> dict[str, object]:
        return self.health_payload

    def send(self, who: str, message: str) -> dict[str, object]:
        self.sent.append((who, message))
        return {"sent": True}

    def ack_event(self, event_id: str) -> dict[str, object]:
        self.acked.append(event_id)
        return {"status": "ok", "batch_id": event_id, "batch_status": "submitted"}

    def complete_event(self, event_id: str) -> dict[str, object]:
        self.completed.append(event_id)
        return {"status": "ok", "batch_id": event_id, "batch_status": "completed"}


class _PollingBridge(_FakeBridge):
    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.payload = payload or {"events": []}
        self.error = error
        self.polls: list[tuple[float, int]] = []

    def poll_events(self, timeout: float, limit: int) -> dict[str, object]:
        self.polls.append((timeout, limit))
        if self.error is not None:
            raise self.error
        return self.payload


class _FakeHermes:
    def __init__(self, reply: str = " reply ") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def ask(self, prompt: str, *, session_name: str) -> str:
        self.calls.append((prompt, session_name))
        return self.reply


class _RaisingHermes:
    def ask(self, prompt: str, *, session_name: str) -> str:
        raise RuntimeError("process bug")


def test_format_prompt_includes_chat_and_messages() -> None:
    prompt = format_prompt(
        {
            "chat_name": "测试群",
            "messages": [
                {"time_text": "09:30", "sender": "Alice", "content": "早上好"},
                {"time_text": None, "sender": None, "is_self": True, "content": " 我来处理 "},
                {"sender": None, "is_self": False, "content": "谢谢"},
                {"sender": "Ignored", "content": ""},
                {"sender": "Blank", "content": "   "},
                "not a message",
            ],
        }
    )

    assert "你正在作为微信机器人回复一个会话。" in prompt
    assert "会话名：测试群" in prompt
    assert "本次收到的新消息：" in prompt
    assert "- 09:30 Alice: 早上好" in prompt
    assert "- 我: 我来处理" in prompt
    assert "- 对方: 谢谢" in prompt
    assert "Ignored" not in prompt
    assert "Blank" not in prompt
    assert "not a message" not in prompt
    assert "会话名：测试群\n\n本次收到的新消息：" in prompt
    assert "谢谢\n\n请只输出要发送到微信的回复文本。" in prompt
    assert prompt.endswith("请只输出要发送到微信的回复文本。不要解释，不要包含前后缀。")


def test_session_name_for_chat_is_stable_and_ascii() -> None:
    first = session_name_for_chat("测试群")
    second = session_name_for_chat("测试群")

    assert first == second
    assert first.startswith("wxauto-")
    assert first.isascii()
    assert len(first) <= 48


def test_format_prompt_normalizes_none_chat_name() -> None:
    prompt = format_prompt({"chat_name": None, "messages": []})

    assert "会话名：" in prompt
    assert "会话名：None" not in prompt


def test_session_name_for_chat_normalizes_non_string_values() -> None:
    none_name = session_name_for_chat(None)
    numeric_name = session_name_for_chat(123)

    assert none_name == session_name_for_chat(None)
    assert numeric_name == session_name_for_chat(123)
    assert none_name.startswith("wxauto-")
    assert numeric_name.startswith("wxauto-")
    assert none_name.isascii()
    assert numeric_name.isascii()
    assert len(none_name) <= 48
    assert len(numeric_name) <= 48


def test_bridge_client_gets_health(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_urlopen(req: object, timeout: float) -> _Response:
        calls.append((req, timeout))
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(hermes_sidecar.request, "urlopen", fake_urlopen)

    client = BridgeClient("http://127.0.0.1:8765/", timeout=3.0)
    result = client.health()

    req, timeout = calls[0]
    assert result == {"ok": True}
    assert req.full_url == "http://127.0.0.1:8765/health"
    assert req.get_method() == "GET"
    assert timeout == 3.0


def test_bridge_client_polls_events(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_urlopen(req: object, timeout: float) -> _Response:
        calls.append((req, timeout))
        return _Response(b'{"events": []}')

    monkeypatch.setattr(hermes_sidecar.request, "urlopen", fake_urlopen)

    client = BridgeClient("http://bridge", timeout=2.5)
    result = client.poll_events(timeout=30.0, limit=10)

    req, request_timeout = calls[0]
    assert result == {"events": []}
    assert req.full_url == "http://bridge/events?timeout=30.0&limit=10"
    assert req.get_method() == "GET"
    assert request_timeout == 32.5


def test_bridge_client_sends_message(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_urlopen(req: object, timeout: float) -> _Response:
        calls.append((req, timeout))
        return _Response(b'{"sent": true}')

    monkeypatch.setattr(hermes_sidecar.request, "urlopen", fake_urlopen)

    client = BridgeClient("http://bridge", timeout=4.0)
    result = client.send("张三", "你好")

    req, timeout = calls[0]
    assert result == {"sent": True}
    assert req.full_url == "http://bridge/send"
    assert req.get_method() == "POST"
    assert json.loads(req.data.decode("utf-8")) == {"who": "张三", "message": "你好"}
    assert "你好".encode("utf-8") in req.data
    headers = {k.lower(): v for k, v in req.headers.items()}
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert timeout == 60.0


def test_bridge_client_acks_and_completes_events(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_urlopen(req: object, timeout: float) -> _Response:
        calls.append((req, timeout))
        return _Response(b'{"status": "ok"}')

    monkeypatch.setattr(hermes_sidecar.request, "urlopen", fake_urlopen)

    client = BridgeClient("http://bridge", timeout=4.0)
    ack = client.ack_event("batch-1")
    complete = client.complete_event("batch-1")

    assert ack == {"status": "ok"}
    assert complete == {"status": "ok"}
    assert [call[0].full_url for call in calls] == [
        "http://bridge/events/batch-1/ack",
        "http://bridge/events/batch-1/complete",
    ]
    assert [call[0].get_method() for call in calls] == ["POST", "POST"]


def test_bridge_client_rejects_non_object_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> _Response:
        return _Response(b'[]')

    monkeypatch.setattr(hermes_sidecar.request, "urlopen", fake_urlopen)

    client = BridgeClient("http://bridge")

    with pytest.raises(hermes_sidecar.BridgeRequestError, match="bridge response must be a JSON object"):
        client.health()


def test_hermes_runner_invokes_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(
        args: list[str],
        *,
        text: bool,
        encoding: str,
        errors: str,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(
            {
                "args": args,
                "text": text,
                "encoding": encoding,
                "errors": errors,
                "capture_output": capture_output,
                "timeout": timeout,
                "check": check,
            }
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=" answer \n")

    monkeypatch.setattr(hermes_sidecar.subprocess, "run", fake_run)

    runner = hermes_sidecar.HermesRunner(("wsl.exe", "hermes"), timeout=12.5)
    result = runner.ask("hello", session_name="wxauto-session")

    assert result == "answer"
    assert calls == [
        {
            "args": [
                "wsl.exe",
                "hermes",
                "chat",
                "-q",
                "hello",
                "-Q",
                "--source",
                "tool",
            ],
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "capture_output": True,
            "timeout": 12.5,
            "check": True,
        }
    ]


def test_hermes_runner_wraps_wsl_bash_lc_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(
        args: list[str],
        *,
        text: bool,
        encoding: str,
        errors: str,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(
            {
                "args": args,
                "text": text,
                "encoding": encoding,
                "errors": errors,
                "capture_output": capture_output,
                "timeout": timeout,
                "check": check,
            }
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=" ok \n")

    monkeypatch.setattr(hermes_sidecar.subprocess, "run", fake_run)

    runner = hermes_sidecar.HermesRunner(("wsl.exe", "bash", "-lc", "hermes"), timeout=12.5)
    prompt = "第一行\n第二行"
    result = runner.ask(prompt, session_name="wxauto-session")

    expected_shell_command = " ".join(
        shlex.quote(part)
        for part in [
            "hermes",
            "chat",
            "-q",
            prompt,
            "-Q",
            "--source",
            "tool",
        ]
    )
    assert result == "ok"
    assert calls == [
        {
            "args": ["wsl.exe", "bash", "-lc", expected_shell_command],
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "capture_output": True,
            "timeout": 12.5,
            "check": True,
        }
    ]


def test_default_hermes_command_uses_wsl_login_shell() -> None:
    assert hermes_sidecar.SidecarConfig().hermes_command == ("wsl.exe", "bash", "-lc", "hermes")
    args = hermes_sidecar.build_parser().parse_args([])
    assert hermes_sidecar._split_command(args.hermes_command) == ("wsl.exe", "bash", "-lc", "hermes")


def test_sidecar_processes_event_and_sends_reply() -> None:
    bridge = _FakeBridge()
    hermes = _FakeHermes(" hello there \n")
    sidecar = hermes_sidecar.HermesSidecar(hermes_sidecar.SidecarConfig(), bridge=bridge, hermes=hermes)

    result = sidecar.process_event({"batch_id": "batch-1", "chat_name": " Test Chat ", "messages": [{"content": "hi"}]})

    assert result == "hello there"
    assert bridge.sent == [("Test Chat", "hello there")]
    assert bridge.acked == ["batch-1"]
    assert bridge.completed == ["batch-1"]
    assert hermes.calls
    prompt, session_name = hermes.calls[0]
    assert "Test Chat" in prompt
    assert session_name == session_name_for_chat("Test Chat")


def test_sidecar_dry_run_does_not_send_reply(capsys: pytest.CaptureFixture[str]) -> None:
    bridge = _FakeBridge()
    hermes = _FakeHermes(" dry reply ")
    config = hermes_sidecar.SidecarConfig(dry_run=True)
    sidecar = hermes_sidecar.HermesSidecar(config, bridge=bridge, hermes=hermes)

    result = sidecar.process_event({"batch_id": "batch-1", "chat_name": "Room", "messages": [{"content": "hi"}]})

    assert result == "dry reply"
    assert bridge.sent == []
    assert bridge.acked == []
    assert bridge.completed == []
    assert capsys.readouterr().out == "[dry-run] Room: dry reply\n"


def test_sidecar_empty_reply_does_not_send() -> None:
    bridge = _FakeBridge()
    hermes = _FakeHermes(" \n ")
    sidecar = hermes_sidecar.HermesSidecar(hermes_sidecar.SidecarConfig(), bridge=bridge, hermes=hermes)

    result = sidecar.process_event({"batch_id": "batch-1", "chat_name": "Room", "messages": [{"content": "hi"}]})

    assert result is None
    assert bridge.sent == []
    assert bridge.acked == ["batch-1"]
    assert bridge.completed == ["batch-1"]


def test_sidecar_check_health_rejects_unhealthy_bridge() -> None:
    bridge = _FakeBridge({"status": "starting"})
    sidecar = hermes_sidecar.HermesSidecar(hermes_sidecar.SidecarConfig(), bridge=bridge, hermes=_FakeHermes())

    with pytest.raises(RuntimeError, match="bridge is not healthy"):
        sidecar.check_health()


def test_sidecar_blank_chat_name_does_not_call_hermes() -> None:
    bridge = _FakeBridge()
    hermes = _FakeHermes("reply")
    sidecar = hermes_sidecar.HermesSidecar(hermes_sidecar.SidecarConfig(), bridge=bridge, hermes=hermes)

    result = sidecar.process_event({"chat_name": "   ", "messages": [{"content": "hi"}]})

    assert result is None
    assert hermes.calls == []
    assert bridge.sent == []


def test_sidecar_missing_chat_name_does_not_call_hermes() -> None:
    bridge = _FakeBridge()
    hermes = _FakeHermes("reply")
    sidecar = hermes_sidecar.HermesSidecar(hermes_sidecar.SidecarConfig(), bridge=bridge, hermes=hermes)

    result = sidecar.process_event({"messages": [{"content": "hi"}]})

    assert result is None
    assert hermes.calls == []
    assert bridge.sent == []


def test_sidecar_run_once_processes_polled_events() -> None:
    bridge = _PollingBridge(
        {
            "events": [
                {"chat_name": "Room 1", "messages": [{"content": "hi"}]},
                {"chat_name": "Room 2", "messages": [{"content": "hello"}]},
            ]
        }
    )
    hermes = _FakeHermes("reply")
    config = hermes_sidecar.SidecarConfig(poll_timeout=4.5, poll_limit=2, once=True)
    sidecar = hermes_sidecar.HermesSidecar(config, bridge=bridge, hermes=hermes)

    sidecar.run()

    assert bridge.polls == [(4.5, 2)]
    assert bridge.sent == [("Room 1", "reply"), ("Room 2", "reply")]
    assert len(hermes.calls) == 2


def test_sidecar_run_once_skips_non_dict_events() -> None:
    bridge = _PollingBridge({"events": ["bad", {"chat_name": "Room", "messages": [{"content": "hi"}]}, 123]})
    hermes = _FakeHermes("reply")
    sidecar = hermes_sidecar.HermesSidecar(
        hermes_sidecar.SidecarConfig(once=True),
        bridge=bridge,
        hermes=hermes,
    )

    sidecar.run()

    assert bridge.sent == [("Room", "reply")]
    assert len(hermes.calls) == 1


def test_sidecar_run_once_does_not_swallow_process_event_runtime_error() -> None:
    bridge = _PollingBridge({"events": [{"chat_name": "Room", "messages": [{"content": "hi"}]}]})
    sidecar = hermes_sidecar.HermesSidecar(
        hermes_sidecar.SidecarConfig(once=True),
        bridge=bridge,
        hermes=_RaisingHermes(),
    )

    with pytest.raises(RuntimeError, match="process bug"):
        sidecar.run()


def test_sidecar_run_once_logs_and_sleeps_on_bridge_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bridge = _PollingBridge(error=hermes_sidecar.BridgeRequestError("boom"))
    sleeps: list[float] = []
    monkeypatch.setattr(hermes_sidecar.time, "sleep", sleeps.append)
    sidecar = hermes_sidecar.HermesSidecar(
        hermes_sidecar.SidecarConfig(once=True, retry_delay=1.25),
        bridge=bridge,
        hermes=_FakeHermes(),
    )

    sidecar.run()

    assert sleeps == [1.25]
    assert capsys.readouterr().out == "[hermes-wxauto hermes-sidecar] error: boom\n"


def test_main_builds_sidecar_config(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeSidecar:
        def __init__(self, config: hermes_sidecar.SidecarConfig) -> None:
            calls.append({"config": config})

        def check_health(self) -> dict[str, object]:
            calls.append({"check_health": True})
            return {"status": "ok"}

        def run(self) -> None:
            calls.append({"run": True})

    monkeypatch.setattr(hermes_sidecar, "HermesSidecar", FakeSidecar)

    result = hermes_sidecar.main(
        [
            "--bridge-url",
            "http://bridge",
            "--poll-timeout",
            "7.5",
            "--poll-limit",
            "9",
            "--hermes-command",
            "wsl.exe  hermes --profile sidecar",
            "--hermes-timeout",
            "15.5",
            "--dry-run",
            "--once",
        ]
    )

    assert result == 0
    assert calls[0]["config"] == hermes_sidecar.SidecarConfig(
        bridge_url="http://bridge",
        poll_timeout=7.5,
        poll_limit=9,
        hermes_command=("wsl.exe", "hermes", "--profile", "sidecar"),
        hermes_timeout=15.5,
        dry_run=True,
        once=True,
        session_file=calls[0]["config"].session_file,
    )
    assert calls[1:] == [{"check_health": True}, {"run": True}]


def test_split_command_filters_empty_parts() -> None:
    assert hermes_sidecar._split_command("  wsl.exe   hermes  chat ") == ("wsl.exe", "hermes", "chat")


def test_split_command_keeps_quoted_windows_path_and_profile_value() -> None:
    command = r'"C:\Program Files\Hermes\hermes.exe" --profile "wx sidecar"'

    result = hermes_sidecar._split_command(command)

    assert result == (r"C:\Program Files\Hermes\hermes.exe", "--profile", "wx sidecar")
