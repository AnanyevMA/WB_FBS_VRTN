"""
Unit & Integration Tests for Knowledge Base Service and KB Sync Agent.
"""
from pathlib import Path
import pytest

from app.agent_manifest import load_manifest
from app.agents.kb_sync_agent import lookup_kb_solution, sync_knowledge_base
from app.services.kb_service import KBService


def test_kb_service_index_loading_and_structure():
    """Verify that INDEX.json is loaded and contains valid categories and documents."""
    service = KBService()
    index_data = service.load_index(force_reload=True)

    assert "version" in index_data
    assert "categories" in index_data
    assert "documents" in index_data
    assert len(index_data["categories"]) >= 4
    assert len(index_data["documents"]) >= 10

    # Verify all expected documents exist
    doc_ids = [d["id"] for d in index_data["documents"]]
    expected_ids = [
        "wb_01_overview_and_auth",
        "wb_02_orders_workflow",
        "wb_03_kiz_and_meta",
        "wb_04_supplies_and_shipment",
        "cz_01_auth_and_cryptography",
        "cz_02_suz_emission_and_orders",
        "cz_03_true_api_withdrawal",
        "cz_04_true_api_returns",
        "cz_05_kiz_structure_and_validation",
        "sol_01_end_to_end_pipeline",
        "sol_02_multiagent_coordination",
        "sol_03_deployment_and_crypto",
        "tb_01_error_catalog",
    ]
    for exp_id in expected_ids:
        assert exp_id in doc_ids, f"Expected document {exp_id} missing from index"


def test_kb_two_tier_fast_search():
    """Verify fast two-tier searching by error code, endpoint, tags, and keywords."""
    service = KBService()

    # 1. Search by error code: 409 Conflict
    res_409 = service.search(error_code="409 Conflict")
    assert len(res_409) > 0
    top_doc = res_409[0]
    assert "03_kiz_and_meta" in top_doc["file"] or "01_error_catalog" in top_doc["file"]

    # 2. Search by endpoint: /api/v3/orders/new
    res_orders = service.search(endpoint="/api/v3/orders/new")
    assert len(res_orders) > 0
    assert "02_orders_workflow" in res_orders[0]["file"]

    # 3. Search by tag: ukep
    res_ukep = service.search(tags=["ukep"])
    assert len(res_ukep) > 0
    assert "01_auth_and_cryptography" in res_ukep[0]["file"]

    # 4. Search by query: 'списание' / 'withdrawal'
    res_query = service.search(query="вывод из оборота")
    assert len(res_query) > 0
    assert any("withdrawal" in d["file"] for d in res_query)


def test_kb_get_document_content():
    """Verify loading specific document content directly by id or relative path."""
    service = KBService()
    content = service.get_document_content("wb_03_kiz_and_meta")
    assert content is not None
    assert "Wildberries Marketplace API v3: Привязка КИЗ" in content
    assert "PUT /api/v3/orders/{orderId}/meta/sgtin" in content


def test_kb_integrity_validation():
    """Verify that all markdown files exist and have healthy links."""
    service = KBService()
    report = service.validate_integrity()
    assert report["status"] == "HEALTHY", f"Integrity check failed: {report.get('issues')}"
    assert report["checked_documents_count"] >= 10
    assert len(report["issues"]) == 0


def test_kb_sync_agent_execution():
    """Verify that the Celery agent task executes and returns valid status."""
    result = sync_knowledge_base()
    assert isinstance(result, dict)
    assert result["status"] == "HEALTHY"
    assert result["checked_count"] >= 10


def test_lookup_kb_solution_helper():
    """Verify the lookup helper used by other agents."""
    results = lookup_kb_solution(query="Rate limit 429", limit=2)
    assert len(results) > 0
    assert "match_score" in results[0]
    assert results[0]["match_score"] > 0


def test_kb_sync_agent_manifest_registration():
    """Verify kb_sync_agent is registered in agents_config.json with correct permissions."""
    manifest_path = Path(__file__).resolve().parent.parent / "agents_config.json"
    manifest = load_manifest(manifest_path)

    agent = manifest.get_agent("kb_sync_agent")
    assert agent is not None
    assert agent.queue == "maintenance"
    assert agent.enabled is True
    assert "docs/*" in agent.polp_matrix.allowed_read_paths
    assert "docs/INDEX.json" in agent.polp_matrix.allowed_write_paths
