"""Цена заказа WB: рубли из convertedPrice, а не цена в валюте покупателя."""
import json
from decimal import Decimal

import pytest

from app.services.cz_client import CZClient
from app.services.wb_prices import price_to_kopecks, wb_order_price_rub


def test_rub_order_uses_price():
    assert wb_order_price_rub({"price": 678500, "currencyCode": 643}) == Decimal("6785.00")


def test_kazakh_order_uses_converted_price():
    raw = {"id": 6287297711, "price": 3388000, "currencyCode": 398, "convertedPrice": 678500, "convertedCurrencyCode": 643}
    assert wb_order_price_rub(raw) == Decimal("6785.00")


def test_belarus_order_uses_converted_price():
    raw = {"price": 13955, "currencyCode": 933, "convertedPrice": 360800, "convertedCurrencyCode": 643}
    assert wb_order_price_rub(raw) == Decimal("3608.00")


def test_archive_price_info():
    raw = {"priceInfo": {"price": 2265000, "currencyCode": 398, "convertedPrice": 623174, "convertedCurrencyCode": 643}}
    assert wb_order_price_rub(raw) == Decimal("6231.74")


def test_foreign_currency_without_converted_price_gives_none():
    assert wb_order_price_rub({"id": 1, "price": 3388000, "currencyCode": 398}) is None


def test_converted_price_not_in_rub_gives_none():
    raw = {"price": 3388000, "currencyCode": 398, "convertedPrice": 3388000, "convertedCurrencyCode": 398}
    assert wb_order_price_rub(raw) is None


def test_cheap_item_is_always_divided_by_100():
    # Старое правило "делить только если больше 10000" сохраняло 90 ₽ как 9000 ₽
    assert wb_order_price_rub({"price": 9000, "currencyCode": 643}) == Decimal("90.00")


def test_missing_currency_is_treated_as_rub():
    assert wb_order_price_rub({"price": 150000}) == Decimal("1500.00")


@pytest.mark.parametrize("raw", [{}, {"price": 0}, {"price": None}, {"priceInfo": {}}])
def test_no_price_gives_none(raw):
    assert wb_order_price_rub(raw) is None


@pytest.mark.parametrize("price,expected", [
    (Decimal("6785.00"), 678500), (3563.63, 356363), ("4156.9", 415690), (None, None), (0, None), (-1, None),
])
def test_price_to_kopecks(price, expected):
    assert price_to_kopecks(price) == expected


def test_withdrawal_document_refuses_zero_cost():
    client = CZClient(inn="7701234567", token="mock-token")
    with pytest.raises(ValueError):
        client._build_withdrawal_document(kiz_codes=["0104630199251318215QTSRh>4sVc+."], price_kopecks=0)
    with pytest.raises(ValueError):
        client._build_withdrawal_document(items=[{"cis": "0104630199251318215QTSRh>4sVc+.", "price_kopecks": 0}])


def test_withdrawal_document_keeps_positive_cost():
    client = CZClient(inn="7701234567", token="mock-token")
    doc = client._build_withdrawal_document(kiz_codes=["0104630199251318215QTSRh>4sVc+."], price_kopecks=678500)
    assert json.loads(doc["productDocument"])["products"][0]["product_cost"] == 678500
