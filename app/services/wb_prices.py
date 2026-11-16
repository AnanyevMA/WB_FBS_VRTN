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


def price_to_kopecks(price: Any) -> Optional[int]:
    """Стоимость для документа ЧЗ в копейках. None, если цены нет: отправлять 0 нельзя."""
    if price is None:
        return None
    try:
        kopecks = int((Decimal(str(price)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None
    return kopecks if kopecks > 0 else None
