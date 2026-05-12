from __future__ import annotations

from my_wxauto.hermes_sidecar import format_prompt, session_name_for_chat


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
