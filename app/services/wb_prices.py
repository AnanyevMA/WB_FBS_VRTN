"""
Цены заказов WB.

В сборочных заданиях WB поле `price` указано в валюте покупателя (тенге, белорусские рубли и т.д.),
цена в рублях продавца приходит в `convertedPrice`. Оба поля умножены на 100.
В архиве заказов те же поля лежат внутри `priceInfo`.
"""
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

RUB_CURRENCY_CODE = 643


def _is_rub(code: Any) -> bool:
    """Отсутствующий код валюты считаем рублями: старые ответы WB его не содержали."""
    if code is None or code == "":
        return True
    try:
        return int(code) == RUB_CURRENCY_CODE
    except (TypeError, ValueError):
        return False


def _from_hundredths(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)) / 100
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0:
        return None
    return amount.quantize(Decimal("0.01"))


def wb_order_price_rub(raw: Dict[str, Any]) -> Optional[Decimal]:
    """Цена заказа в рублях или None, если рублёвой цены в ответе WB нет."""
    info = raw.get("priceInfo") if isinstance(raw.get("priceInfo"), dict) else raw

    if info.get("convertedPrice") is not None and _is_rub(info.get("convertedCurrencyCode")):
        price = _from_hundredths(info.get("convertedPrice"))
        if price is not None:
            return price

    if _is_rub(info.get("currencyCode")):
        return _from_hundredths(info.get("price"))

    logger.warning(
        "WB order %s has no ruble price: currencyCode=%s convertedCurrencyCode=%s",
        raw.get("id"), info.get("currencyCode"), info.get("convertedCurrencyCode"),
    )
    return None


RUB_CURRENCY_NAMES = {"руб", "rub", "rur", "₽"}


def excise_price_rub(row: Dict[str, Any]) -> Optional[float]:
    """
    Цена продажи из отчёта по маркировке WB (excise-report) в рублях или None.

    Отчёт отдаёт цену в валюте страны продажи (поле currency_name_short: руб, BYN, KZT, AMD).
    Цену не в рублях использовать нельзя: в документ ЧЗ ушли бы 120.65 вместо 3 298.86 ₽.
    Пустая валюта считается рублями: так отдавались старые строки отчёта.
    """
    currency = str(row.get("currency_name_short") or "").strip().lower()
    if currency and currency not in RUB_CURRENCY_NAMES:
        return None
    try:
        price = float(row.get("price") or 0.0)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def price_to_kopecks(price: Any) -> Optional[int]:
    """Стоимость для документа ЧЗ в копейках. None, если цены нет: отправлять 0 нельзя."""
    if price is None:
        return None
    try:
        kopecks = int((Decimal(str(price)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None
    return kopecks if kopecks > 0 else None
