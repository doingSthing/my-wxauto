from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error as urlerror, parse, request


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def session_name_for_chat(chat_name: Any) -> str:
    normalized_chat_name = _normalize_text(chat_name)
    digest = hashlib.sha1(normalized_chat_name.encode("utf-8")).hexdigest()[:16]
    return f"wxauto-{digest}"


class BridgeRequestError(RuntimeError):
    pass


class BridgeClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self._request_json("GET", f"{self.base_url}/health")

    def poll_events(self, timeout: float, limit: int) -> dict[str, Any]:
        query = parse.urlencode({"timeout": timeout, "limit": limit})
        return self._request_json(
            "GET",
            f"{self.base_url}/events?{query}",
            timeout=timeout + self.timeout,
        )

    def send(self, who: str, message: str) -> dict[str, Any]:
        body = json.dumps({"who": who, "message": message}, ensure_ascii=False).encode("utf-8")
        return self._request_json(
            "POST",
            f"{self.base_url}/send",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        req = request.Request(url, data=data, headers=headers or {}, method=method)
        with request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as response:
            result = json.loads(response.read().decode("utf-8"))

        if not isinstance(result, dict):
            raise BridgeRequestError("bridge response must be a JSON object")

        return result


class BridgeLike(Protocol):
    def health(self) -> dict[str, Any]:
        ...

    def poll_events(self, timeout: float, limit: int) -> dict[str, Any]:
        ...

    def send(self, who: str, message: str) -> dict[str, Any]:
        ...


class HermesLike(Protocol):
    def ask(self, prompt: str, *, session_name: str) -> str:
        ...


@dataclass(frozen=True)
class SidecarConfig:
    bridge_url: str = "http://127.0.0.1:8765"
    poll_timeout: float = 30.0
    poll_limit: int = 5
    hermes_command: tuple[str, ...] = ("wsl.exe", "hermes")
    hermes_timeout: float = 120.0
    dry_run: bool = False
    once: bool = False
    retry_delay: float = 3.0


class HermesRunner:
    def __init__(self, command: Sequence[str], *, timeout: float) -> None:
        self.command = tuple(command)
        self.timeout = timeout

    def ask(self, prompt: str, *, session_name: str) -> str:
        result = subprocess.run(
            [
                *self.command,
                "chat",
                "-q",
                prompt,
                "-Q",
                "--continue",
                session_name,
                "--source",
                "tool",
            ],
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=True,
        )
        return result.stdout.strip()


class HermesSidecar:
    def __init__(
        self,
        config: SidecarConfig,
        bridge: BridgeLike | None = None,
        hermes: HermesLike | None = None,
    ) -> None:
        self.config = config
        self.bridge = bridge if bridge is not None else BridgeClient(config.bridge_url)
        self.hermes = hermes if hermes is not None else HermesRunner(
            config.hermes_command,
            timeout=config.hermes_timeout,
        )

    def check_health(self) -> dict[str, Any]:
        payload = self.bridge.health()
        if payload.get("status") != "ok":
            raise RuntimeError(f"bridge is not healthy: {payload}")
        return payload

    def process_event(self, event: dict[str, Any]) -> str | None:
        chat_name = _normalize_text(event.get("chat_name")).strip()
        if not chat_name:
            return None

        prompt = format_prompt(event)
        reply = self.hermes.ask(prompt, session_name=session_name_for_chat(chat_name)).strip()
        if not reply:
            return None

        if self.config.dry_run:
            print(f"[dry-run] {chat_name}: {reply}")
            return reply

        self.bridge.send(chat_name, reply)
        return reply

    def run(self) -> None:
        while True:
            try:
                payload = self.bridge.poll_events(
                    timeout=self.config.poll_timeout,
                    limit=self.config.poll_limit,
                )
                events = payload.get("events") or []
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    self.process_event(event)
            except (urlerror.URLError, TimeoutError, BridgeRequestError, subprocess.SubprocessError) as exc:
                print(f"[my-wxauto hermes-sidecar] error: {exc}")
                time.sleep(self.config.retry_delay)

            if self.config.once:
                return


def format_prompt(event: dict[str, Any]) -> str:
    chat_name = _normalize_text(event.get("chat_name"))
    lines = [
        "你正在作为微信机器人回复一个会话。",
        f"会话名：{chat_name}",
        "",
        "本次收到的新消息：",
    ]

    for message in event.get("messages", ()):
        if not isinstance(message, dict):
            continue

        content_value = message.get("content")
        if content_value is None:
            continue

        content = str(content_value).strip()
        if not content:
            continue

        sender = _normalize_text(message.get("sender")).strip()
        if not sender:
            sender = "我" if message.get("is_self") is True else "对方"

        time_text = _normalize_text(message.get("time_text")).strip()
        if time_text:
            lines.append(f"- {time_text} {sender}: {content}")
        else:
            lines.append(f"- {sender}: {content}")

    lines.append("")
    lines.append("请只输出要发送到微信的回复文本。不要解释，不要包含前后缀。")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765")
    parser.add_argument("--poll-timeout", type=float, default=30.0)
    parser.add_argument("--poll-limit", type=int, default=5)
    parser.add_argument("--hermes-command", default="wsl.exe hermes")
    parser.add_argument("--hermes-timeout", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser


def _split_command(value: str) -> tuple[str, ...]:
    return tuple(shlex.split(value, posix=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = SidecarConfig(
        bridge_url=args.bridge_url,
        poll_timeout=args.poll_timeout,
        poll_limit=args.poll_limit,
        hermes_command=_split_command(args.hermes_command),
        hermes_timeout=args.hermes_timeout,
        dry_run=args.dry_run,
        once=args.once,
    )
    sidecar = HermesSidecar(config)
    sidecar.check_health()
    sidecar.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
