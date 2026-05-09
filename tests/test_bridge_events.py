from __future__ import annotations

from my_wxauto.bridge_events import (
    BridgeMessage,
    ConversationBatch,
    make_message_key,
    messages_from_chat_payload,
)


def test_make_message_key_is_stable_for_same_message() -> None:
    first = BridgeMessage(
        chat_name="alice",
        content="hello",
        message_type="text",
        sender="alice",
        is_self=False,
        time_text="15:41",
        occurrence_index=0,
    )
    second = BridgeMessage(
        chat_name="alice",
        content="hello",
        message_type="text",
        sender="alice",
        is_self=False,
        time_text="15:41",
        occurrence_index=0,
    )

    assert make_message_key(first) == make_message_key(second)
    assert len(make_message_key(first)) == 64


def test_make_message_key_uses_occurrence_index_for_repeated_text() -> None:
    first = BridgeMessage(chat_name="alice", content="ok", occurrence_index=0)
    second = BridgeMessage(chat_name="alice", content="ok", occurrence_index=1)

    assert make_message_key(first) != make_message_key(second)


def test_messages_from_chat_payload_adds_keys_and_occurrence_indexes() -> None:
    messages = messages_from_chat_payload(
        {
            "chat_name": "alice",
            "messages": [
                {"content": "ok", "message_type": "text", "sender": "alice"},
                {"content": "ok", "message_type": "text", "sender": "alice"},
            ],
        }
    )

    assert [message.occurrence_index for message in messages] == [0, 1]
    assert messages[0].message_key != messages[1].message_key
    assert messages[0].to_dict()["chat_name"] == "alice"


def test_messages_from_chat_payload_key_ignores_unrelated_preceding_message() -> None:
    without_preceding = messages_from_chat_payload(
        {
            "chat_name": "alice",
            "messages": [
                {
                    "content": "target",
                    "message_type": "text",
                    "sender": "alice",
                    "is_self": False,
                    "time_text": "15:41",
                },
            ],
        }
    )
    with_preceding = messages_from_chat_payload(
        {
            "chat_name": "alice",
            "messages": [
                {
                    "content": "unrelated",
                    "message_type": "text",
                    "sender": "alice",
                    "is_self": False,
                    "time_text": "15:40",
                },
                {
                    "content": "target",
                    "message_type": "text",
                    "sender": "alice",
                    "is_self": False,
                    "time_text": "15:41",
                },
            ],
        }
    )

    assert without_preceding[0].occurrence_index == 0
    assert with_preceding[1].occurrence_index == 0
    assert without_preceding[0].message_key == with_preceding[1].message_key


def test_messages_from_chat_payload_distinguishes_identical_repeated_messages() -> None:
    messages = messages_from_chat_payload(
        {
            "chat_name": "alice",
            "messages": [
                {
                    "content": "same",
                    "message_type": "text",
                    "sender": "alice",
                    "is_self": False,
                    "time_text": "15:41",
                },
                {
                    "content": "same",
                    "message_type": "text",
                    "sender": "alice",
                    "is_self": False,
                    "time_text": "15:41",
                },
            ],
        }
    )

    assert [message.occurrence_index for message in messages] == [0, 1]
    assert messages[0].message_key != messages[1].message_key


def test_messages_from_chat_payload_skips_invalid_entries_and_preserves_raw() -> None:
    raw_payload = {"raw_name": "fallback", "message_type": "image", "sender": "alice"}
    messages = messages_from_chat_payload(
        {
            "chat_name": "alice",
            "messages": [
                "not a dict",
                {"content": ""},
                {"content": "   "},
                raw_payload,
                {"content": " \t ", "raw_name": "blank-content-fallback"},
                {"content": " keep spaces ", "message_type": "text"},
            ],
        }
    )

    assert [message.content for message in messages] == [
        "fallback",
        "blank-content-fallback",
        " keep spaces ",
    ]
    assert messages[0].raw is raw_payload
    assert messages[0].to_dict()["raw"] == raw_payload


def test_conversation_batch_to_event_dict() -> None:
    message = BridgeMessage(
        chat_name="alice",
        content="hello",
        sender="alice",
        occurrence_index=0,
    ).with_key()
    batch = ConversationBatch(
        batch_id="batch-1",
        chat_name="alice",
        messages=(message,),
        created_at=10.0,
        frozen_at=11.0,
        status="frozen",
    )

    payload = batch.to_event_dict()

    assert payload["batch_id"] == "batch-1"
    assert payload["event_id"] == "batch-1"
    assert payload["platform"] == "wechat_desktop"
    assert payload["chat_id"] == "wechat:alice"
    assert payload["messages"][0]["content"] == "hello"
