"""
Unit tests for Telegram chat filtering and manager chat routing
"""
from types import SimpleNamespace
from app.services.telegram_service import (
    is_group_chat,
    is_private_chat,
    filter_private_chats,
    get_personal_manager_chats,
)


def test_is_group_chat():
    assert is_group_chat("-1002498223661") is True
    assert is_group_chat("-12345678") is True
    assert is_group_chat(-1002498223661) is True
    assert is_group_chat(" -1002498223661 ") is True

    assert is_group_chat("411702261") is False
    assert is_group_chat(411702261) is False
    assert is_group_chat(None) is False
    assert is_group_chat("") is False


def test_is_private_chat():
    assert is_private_chat("411702261") is True
    assert is_private_chat(411702261) is True
    assert is_private_chat(" 411702261 ") is True

    assert is_private_chat("-1002498223661") is False
    assert is_private_chat(-1002498223661) is False
    assert is_private_chat(" -1002498223661 ") is False
    assert is_private_chat(None) is False
    assert is_private_chat("") is False


def test_filter_private_chats():
    raw_chats = ["411702261", "-1002498223661", 12345, -99999, " 411702261 ", None, ""]
    filtered = filter_private_chats(raw_chats)
    assert filtered == ["411702261", "12345"]


def test_get_personal_manager_chats():
    # Case 1: auto_kiz_manager_chat_id is set + group and personal in telegram_chat_ids
    seller1 = SimpleNamespace(
        auto_kiz_manager_chat_id="411702261",
        telegram_chat_ids=["411702261", "-1002498223661", "987654321"]
    )
    chats1 = get_personal_manager_chats(seller1)
    assert chats1 == ["411702261", "987654321"]
    assert "-1002498223661" not in chats1

    # Case 2: Only group chats configured -> returns empty list
    seller2 = SimpleNamespace(
        auto_kiz_manager_chat_id=None,
        telegram_chat_ids=["-1002498223661", "-200300400"]
    )
    chats2 = get_personal_manager_chats(seller2)
    assert chats2 == []

    # Case 3: auto_kiz_manager_chat_id is a group (anomaly) -> should be excluded
    seller3 = SimpleNamespace(
        auto_kiz_manager_chat_id="-1009999999",
        telegram_chat_ids=["555666777"]
    )
    chats3 = get_personal_manager_chats(seller3)
    assert chats3 == ["555666777"]
