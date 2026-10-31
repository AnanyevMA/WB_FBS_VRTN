"""
Frontend scripts share one global scope, so a repeated top-level let/const stops the whole
second file from loading. That silently disabled kiz.js: the scanner and signing buttons.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.kiz.documents import withdraw_kiz

FRONTEND = Path("frontend")
TOP_LEVEL_DECLARATION = re.compile(r"^(?:let|const|class)\s+([A-Za-z_$][\w$]*)", re.M)


def _loaded_scripts() -> list[Path]:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    sources = re.findall(r'<script[^>]+src="(js/[^"?]+)', html)
    assert sources, "index.html loads no local scripts"
    return [FRONTEND / src for src in sources]


def test_scripts_do_not_redeclare_top_level_names():
    seen: dict[str, str] = {}
    duplicates = []
    for path in _loaded_scripts():
        for name in TOP_LEVEL_DECLARATION.findall(path.read_text(encoding="utf-8")):
            if name in seen:
                duplicates.append(f"{name}: {seen[name]} and {path.name}")
            seen[name] = path.name
    assert duplicates == []


def test_signing_state_is_declared_before_kiz_script():
    names = [p.name for p in _loaded_scripts()]
    assert names.index("state.js") < names.index("kiz.js")
    assert "currentSigningPayload" in TOP_LEVEL_DECLARATION.findall((FRONTEND / "js/state.js").read_text(encoding="utf-8"))


def _run_candidate_filter(function_name: str, orders: list[dict]) -> list[int]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = """
const vm = require('vm');
const fs = require('fs');
const context = {window: {}, document: {}, console};
vm.createContext(context);
vm.runInContext('let currentSigningPayload = null;', context);
vm.runInContext(fs.readFileSync('frontend/js/kiz.js', 'utf-8'), context);
const orders = JSON.parse(process.argv[1]);
const check = vm.runInContext(process.argv[2], context);
console.log(JSON.stringify(orders.filter(check).map(o => o.id)));
"""
    result = subprocess.run(
        [node, "-e", script, json.dumps(orders), function_name],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


ORDERS = [
    {"id": 1, "kiz_code": "K1", "kiz_status": "VALIDATED", "status": "DELIVERED", "wb_status": "sold", "cz_withdrawal_doc_id": None},
    {"id": 2, "kiz_code": "K2", "kiz_status": "ATTACHED", "status": "DELIVERED", "wb_status": None, "cz_withdrawal_doc_id": None},
    {"id": 3, "kiz_code": "K3", "kiz_status": "VALIDATED", "status": "DELIVERED", "wb_status": "sold", "cz_withdrawal_doc_id": "doc"},
    {"id": 4, "kiz_code": "K4", "kiz_status": "VALIDATED", "status": "DELIVERING", "wb_status": "on_way", "cz_withdrawal_doc_id": None},
    {"id": 5, "kiz_code": None, "kiz_status": "VALIDATED", "status": "DELIVERED", "wb_status": "sold", "cz_withdrawal_doc_id": None},
    {"id": 6, "kiz_code": "K6", "kiz_status": "WITHDRAWN", "status": "DELIVERED", "wb_status": "sold", "cz_withdrawal_doc_id": "doc"},
    {"id": 7, "kiz_code": "K7", "kiz_status": "WITHDRAWN", "status": "CANCELLED", "wb_status": "canceled", "cz_withdrawal_doc_id": "doc"},
    {"id": 8, "kiz_code": "K8", "kiz_status": "VALIDATED", "status": "CANCELLED", "wb_status": "canceled", "cz_withdrawal_doc_id": None},
    {"id": 9, "kiz_code": "K9", "kiz_status": "WITHDRAWN", "status": "SORTED", "wb_status": "declined_by_client", "cz_withdrawal_doc_id": "doc"},
]


def test_withdrawal_candidates_are_sold_and_not_withdrawn():
    assert _run_candidate_filter("isWithdrawalCandidate", ORDERS) == [1, 2]


def test_return_candidates_exclude_sold_orders():
    assert _run_candidate_filter("isReturnCandidate", ORDERS) == [7, 9]


def _db_returning(*objects) -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(side_effect=list(objects))
    return db


@pytest.mark.asyncio
async def test_withdraw_with_empty_list_is_rejected_and_touches_nothing():
    db = _db_returning(MagicMock())
    with patch("app.agents.cz_withdrawal.withdraw_order_kiz.delay") as delay:
        with pytest.raises(HTTPException) as exc:
            await withdraw_kiz(seller_id="s1", order_ids=[], price_kopecks=0, db=db)
    assert exc.value.status_code == 400
    delay.assert_not_called()
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdraw_processes_only_listed_orders():
    order = MagicMock(seller_id="s1", kiz_code="K1", price=100)
    db = _db_returning(MagicMock(), order)
    with patch("app.agents.cz_withdrawal.withdraw_order_kiz.delay") as delay:
        res = await withdraw_kiz(seller_id="s1", order_ids=[11], price_kopecks=0, db=db)
    assert res["order_ids"] == [11]
    delay.assert_called_once()
    db.execute.assert_not_awaited()
