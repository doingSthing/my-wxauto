from __future__ import annotations


def test_package_exports_listen_conversation_batches() -> None:
    from my_wxauto import BridgeMessage, ConversationBatch, listen_conversation_batches

    assert callable(listen_conversation_batches)
    assert BridgeMessage.__name__ == "BridgeMessage"
    assert ConversationBatch.__name__ == "ConversationBatch"
