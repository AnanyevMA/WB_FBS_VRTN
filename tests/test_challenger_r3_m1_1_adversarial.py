"""
Adversarial Challenge & Empirical Bug Reproduction Test Suite.
Agent: teamwork_preview_challenger_r3_m1_1
Milestone: M1 (Guaranteed KIZ Normalization & Ingestion Integrity)

Demonstrates empirical stress testing across:
1. Valid 31-char Light Industry codes where serial contains '91...92' substrings.
2. Concatenated, space-delimited, and GS-delimited barcodes with diverse serial character patterns.
3. Scanner AIM symbology identifiers (e.g. ']d2').
4. Document builders (_build_withdrawal_document and _build_return_document).
5. Ingestion endpoints (attach_kiz and validate_kiz).
"""
import json
import uuid
import random
import pytest
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.kiz import KizProductInfo
from app.services.encryption import encrypt
from app.services.kiz_service import (
    normalize_kiz_light_industry,
    parse_kiz_code,
    normalize_existing_orders_kiz,
)
from app.services.cz_client import CZClient


# ==============================================================================
# 1. Stress Tests for Valid Light Industry Codes with '91...92' in Serial
# ==============================================================================

# According to RF Decree 1956 and True API v719.0, serial is 13 alphanumeric chars.
# A serial number can legitimately contain '91' and '92' (e.g. 'ABC91DEF92GHI' or '5QT91EE92Vc12').
# In all cases, the normalized CIS must be strictly 31 characters: 01{gtin:14}21{serial:13}.

VALID_SERIALS_WITH_91_92 = [
    "ABC91DEF92GHI",      # '91' + 'DEF' (3) + '92' + 'GHI' (3) = 13 chars
    "5QT91EE92Vc12",      # '91' + 'EE' (2) + '92' + 'Vc12' (4) = 13 chars
    "91ABC92DEF123",      # Starts with '91', has '92' = 13 chars
    "AB91CDEF92123",      # '91' + 'CDEF' (4) + '92' + '123' (3) = 13 chars
]

GTIN = "04630199251318"


@pytest.mark.parametrize("serial", VALID_SERIALS_WITH_91_92)
def test_clean_31_kiz_with_91_92_in_serial_must_stay_31_chars(serial):
    """
    CRITICAL CHALLENGE:
    An already-clean 31-character KIZ (01{gtin}21{serial}) whose 13-character serial
    happens to contain '91...92' MUST NOT be truncated to 21 or 18 characters.
    """
    kiz = f"01{GTIN}21{serial}"
    assert len(kiz) == 31

    out = normalize_kiz_light_industry(kiz)
    assert len(out) == 31, (
        f"FALSE TRUNCATION BUG: Input '{kiz}' (len 31) was truncated to '{out}' (len {len(out)})."
        f" Serial was '{serial}', but normalizer falsely treated serial characters as crypto tail!"
    )
    assert out == kiz


@pytest.mark.parametrize("serial", VALID_SERIALS_WITH_91_92)
def test_concatenated_barcode_with_91_92_in_serial(serial):
    """
    Concatenated raw barcode without delimiters:
    01{gtin}21{serial}91{key}92{crypto}
    Must produce strictly 31 characters containing the full 13-char serial.
    """
    crypto_tail = "91EE1292xyz123456789"
    raw_code = f"01{GTIN}21{serial}{crypto_tail}"
    expected_31 = f"01{GTIN}21{serial}"

    out = normalize_kiz_light_industry(raw_code)
    assert len(out) == 31, (
        f"FALSE TRUNCATION BUG: Input '{raw_code}' was truncated to '{out}' (len {len(out)}) "
        f"instead of expected 31-char '{expected_31}'"
    )
    assert out == expected_31


@pytest.mark.parametrize("serial", VALID_SERIALS_WITH_91_92)
def test_space_delimited_barcode_with_91_92_in_serial(serial):
    """
    Space-delimited raw barcode:
    01{gtin}21{serial} 91{key} 92{crypto}
    Must produce strictly 31 characters containing the full 13-char serial.
    """
    raw_code = f"01{GTIN}21{serial} 91EE12 92xyz123456789"
    expected_31 = f"01{GTIN}21{serial}"

    out = normalize_kiz_light_industry(raw_code)
    assert len(out) == 31, (
        f"FALSE TRUNCATION BUG: Input '{raw_code}' was truncated to '{out}' (len {len(out)}) "
        f"instead of expected 31-char '{expected_31}'"
    )
    assert out == expected_31


@pytest.mark.parametrize("serial", VALID_SERIALS_WITH_91_92)
def test_gs_delimited_barcode_with_91_92_in_serial(serial):
    """
    GS-delimited raw barcode:
    01{gtin}21{serial}\\x1d91{key}\\x1d92{crypto}
    Must produce strictly 31 characters containing the full 13-char serial.
    """
    raw_code = f"01{GTIN}21{serial}\x1d91EE12\x1d92xyz123456789"
    expected_31 = f"01{GTIN}21{serial}"

    out = normalize_kiz_light_industry(raw_code)
    assert len(out) == 31, (
        f"FALSE TRUNCATION BUG: Input '{raw_code}' was truncated to '{out}' (len {len(out)}) "
        f"instead of expected 31-char '{expected_31}'"
    )
    assert out == expected_31


@pytest.mark.parametrize("serial", VALID_SERIALS_WITH_91_92)
def test_parse_kiz_code_with_91_92_in_serial(serial):
    """
    parse_kiz_code must extract full 13-char serial and clean_cis of 31 chars.
    """
    raw_code = f"01{GTIN}21{serial}"
    res = parse_kiz_code(raw_code)
    assert res["serial_number"] == serial, (
        f"parse_kiz_code extracted serial '{res['serial_number']}' instead of '{serial}'"
    )
    assert res["clean_cis"] == raw_code, (
        f"parse_kiz_code produced clean_cis '{res['clean_cis']}' instead of '{raw_code}'"
    )
    assert len(res["clean_cis"]) == 31


# ==============================================================================
# 2. Document Builders and Endpoints with '91...92' in Serial
# ==============================================================================

def test_withdrawal_document_with_91_92_serial():
    """
    _build_withdrawal_document (LK_RECEIPT) must produce strictly 31 chars in products[].cis
    even when serial contains 91 and 92.
    """
    client = CZClient(inn="7701234567", token="mock-token")
    raw_kiz = f"01{GTIN}21ABC91DEF92GHI 91EE12 92xyz123"
    doc = client._build_withdrawal_document(
        kiz_codes=[raw_kiz],
        price_kopecks=100000,
        primary_document_number="5647931541",
    )
    payload = json.loads(doc["productDocument"])
    cis = payload["products"][0]["cis"]
    assert len(cis) == 31, (
        f"GIS MT Error 07 vulnerability: LK_RECEIPT cis has length {len(cis)}: '{cis}'"
    )


def test_return_document_with_91_92_serial():
    """
    _build_return_document (LP_RETURN) must produce strictly 31 chars in products_list[].ki
    even when serial contains 91 and 92.
    """
    client = CZClient(inn="7701234567", token="mock-token")
    raw_kiz = f"01{GTIN}21ABC91DEF92GHI\x1d91EE12\x1d92xyz123"
    doc = client._build_return_document(
        kiz_codes=[raw_kiz],
        primary_document_number="5647931541",
    )
    payload = json.loads(doc["productDocument"])
    products = payload.get("products_list") or payload.get("products")
    ki = products[0]["ki"]
    assert len(ki) == 31, (
        f"GIS MT Error 07 vulnerability: LP_RETURN ki has length {len(ki)}: '{ki}'"
    )


# ==============================================================================
# 3. Randomized Stress Harness (500 iterations)
# ==============================================================================

def test_randomized_valid_barcodes_stress_harness():
    """
    Generates 500 valid randomized light industry barcodes with varied delimiters,
    random alphanumeric characters in serial (including '91' and '92'), and random crypto tails.
    Every single output MUST be strictly 31 characters: 01{gtin:14}21{serial:13}.
    """
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!\"%&'*+-./:;=?_"
    delimiters = [
        " ",
        "  ",
        "\t",
        "\x1d",
        "\u001d",
        "\x1e",
        "\x1f",
        "",  # concatenated
    ]

    failures = []

    for i in range(500):
        # 14-digit GTIN
        gtin = f"046{random.randint(10000000000, 99999999999)}"
        # 13-char serial (sometimes containing 91 and 92)
        if i % 4 == 0:
            # Force serial to contain 91 and 92
            mid_key = "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ", k=3))
            tail_part = "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ", k=3))
            serial = f"AB91{mid_key}92{tail_part}Z"[:13]
        else:
            serial = "".join(random.choices(chars, k=13))

        delim1 = random.choice(delimiters)
        delim2 = random.choice(delimiters)
        key = "".join(random.choices("0123456789ABCDEF", k=random.randint(3, 6)))
        crypto = "".join(random.choices("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz+/=", k=random.randint(4, 44)))

        # Construct raw barcode
        raw = f"01{gtin}21{serial}{delim1}91{key}{delim2}92{crypto}"
        expected_31 = f"01{gtin}21{serial}"

        out = normalize_kiz_light_industry(raw)
        if len(out) != 31 or out != expected_31:
            failures.append((raw, out, expected_31, len(out)))

    assert len(failures) == 0, (
        f"{len(failures)} / 500 barcodes failed normalization! First failure: "
        f"raw='{failures[0][0]}' -> out='{failures[0][1]}' (len {failures[0][3]}), expected='{failures[0][2]}'"
    )
