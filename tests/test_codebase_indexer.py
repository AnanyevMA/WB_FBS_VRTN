"""
Unit & Integration Tests for Codebase Symbol Indexer and Token-Efficient Search Rule.
"""
from pathlib import Path
import pytest

from app.agent_manifest import load_manifest
from app.agents.kb_sync_agent import lookup_code_symbol, sync_knowledge_base
from app.services.codebase_indexer import CodebaseIndexer


def test_codebase_indexer_scan_and_save():
    """Verify that CodebaseIndexer parses the codebase and writes codebase_index.json & CODEBASE_MAP.md."""
    indexer = CodebaseIndexer()
    payload = indexer.scan_project()

    assert payload["total_files"] >= 30
    assert "files" in payload
    assert len(payload["files"]) >= 30

    indexer.save_index(payload)

    # Verify generated files exist
    assert indexer.index_json_path.exists()
    assert indexer.map_md_path.exists()

    map_content = indexer.map_md_path.read_text(encoding="utf-8")
    assert "Карта Архитектуры и Символов Проекта" in map_content
    assert "app/models/seller.py" in map_content
    assert "app/api/sellers.py" in map_content


def test_codebase_indexer_fast_symbol_query():
    """Verify fast token-efficient lookup for classes, functions, and endpoints."""
    indexer = CodebaseIndexer()

    # 1. Query by class name: Seller
    res_seller = indexer.query(symbol="Seller", layer="models")
    assert len(res_seller) > 0
    assert any(c["name"] == "Seller" for c in res_seller[0]["matched_classes"])

    # 2. Query by function: poll_all_sellers
    res_poller = indexer.query(symbol="poll_all_sellers")
    assert len(res_poller) > 0
    assert "order_poller.py" in res_poller[0]["file"]

    # 3. Query by endpoint keyword: /orders
    res_ep = indexer.query(endpoint_keyword="/orders")
    assert len(res_ep) > 0
    assert any("orders" in ep["path"] for ep in res_ep[0]["matched_endpoints"])


def test_lookup_code_symbol_helper():
    """Verify the lookup helper used by agents."""
    results = lookup_code_symbol(symbol="CZClient", limit=3)
    assert len(results) > 0
    assert "cz_client.py" in results[0]["file"]


def test_codebase_indexing_rule_in_manifest():
    """Verify that token_efficient_codebase_indexing rule is enforced in agents_config.json."""
    manifest_path = Path(__file__).resolve().parent.parent / "agents_config.json"
    manifest = load_manifest(manifest_path)

    import json
    with open(manifest_path, encoding="utf-8") as f:
        raw = json.load(f)

    dev_rules = raw.get("development_rules", {})
    assert "token_efficient_codebase_indexing" in dev_rules, "token_efficient_codebase_indexing rule missing"

    rule = dev_rules["token_efficient_codebase_indexing"]
    assert rule.get("enabled") is True
    assert "policy" in rule
    assert "automatic_maintenance_policy" in rule
    assert len(rule.get("required_steps", [])) >= 4

    checklist = dev_rules.get("implementation_checklist", [])
    assert any("Codebase index updated" in item for item in checklist)


def test_kb_and_code_sync_execution():
    """Verify that sync_knowledge_base Celery task syncs both docs and codebase."""
    result = sync_knowledge_base(force_rebuild=True)
    assert isinstance(result, dict)
    assert result["status"] == "HEALTHY"
    assert result["codebase_files_count"] >= 30
