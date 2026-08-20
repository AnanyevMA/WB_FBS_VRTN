"""
Unit tests for agents_config.json and app.agent_manifest PoLPEnforcer.
"""
from pathlib import Path


from app.agent_manifest import (
    AgentsManifest,
    PoLPEnforcer,
    load_manifest,
)


def test_load_manifest():
    manifest_path = Path(__file__).resolve().parent.parent / "agents_config.json"
    manifest = load_manifest(manifest_path)

    assert isinstance(manifest, AgentsManifest)
    # Version must be at least 1.1.0
    major, minor, *_ = manifest.version.split(".")
    assert (int(major), int(minor)) >= (1, 1), f"Expected manifest version >= 1.1.0, got {manifest.version}"
    assert len(manifest.agents) >= 10

    agent_ids = manifest.list_agent_ids()
    expected_agents = [
        "order_poller",
        "supply_agent",
        "cz_withdrawal",
        "cz_return",
        "archive_processor",
        "notifier",
        "cleanup",
        "qa_test_agent",
        "cz_token_refresher",
        "morning_digest",
    ]
    for expected in expected_agents:
        assert expected in agent_ids, f"Expected agent {expected} missing from manifest"


def test_development_rules_holistic_and_test_policies():
    """Verify holistic_impact_analysis, post_change_test_analysis, and github_sync_and_documentation_policy."""
    import json
    manifest_path = Path(__file__).resolve().parent.parent / "agents_config.json"
    with open(manifest_path, encoding="utf-8") as f:
        raw = json.load(f)

    dev_rules = raw.get("development_rules", {})
    assert dev_rules.get("frontend_coverage_required") is True
    assert "holistic_impact_analysis" in dev_rules, "holistic_impact_analysis rule missing"
    assert "post_change_test_analysis" in dev_rules, "post_change_test_analysis rule missing"
    assert "github_sync_and_documentation_policy" in dev_rules, "github_sync_and_documentation_policy rule missing"
    assert "data_and_credential_persistence_policy" in dev_rules, "data_and_credential_persistence_policy rule missing"

    holistic = dev_rules["holistic_impact_analysis"]
    assert holistic.get("enabled") is True
    assert len(holistic.get("layers_to_scan", [])) >= 10

    post_test = dev_rules["post_change_test_analysis"]
    assert post_test.get("enabled") is True
    assert len(post_test.get("required_post_implementation_steps", [])) >= 5

    gh_policy = dev_rules["github_sync_and_documentation_policy"]
    assert gh_policy.get("enabled") is True
    assert len(gh_policy.get("required_steps", [])) >= 5
    assert "documentation_targets" in gh_policy

    cred_policy = dev_rules["data_and_credential_persistence_policy"]
    assert cred_policy.get("enabled") is True
    assert len(cred_policy.get("required_steps", [])) >= 4

    checklist = dev_rules.get("implementation_checklist", [])
    checklist_str = " ".join(checklist)
    assert "holistic project scan" in checklist_str
    assert "test analysis" in checklist_str
    assert "Instructions & docs updated" in checklist_str
    assert "GitHub sync" in checklist_str
    assert "Data & credentials preserved" in checklist_str


def test_polp_enforcer_global_forbidden():
    manifest_path = Path(__file__).resolve().parent.parent / "agents_config.json"
    manifest = load_manifest(manifest_path)
    enforcer = PoLPEnforcer(manifest)

    # .env files are globally forbidden for all agents
    assert enforcer.is_forbidden_globally(".env") is True
    assert enforcer.is_forbidden_globally(".env.production") is True
    assert enforcer.can_read("order_poller", ".env") is False
    assert enforcer.can_write("order_poller", ".env") is False


def test_polp_enforcer_agent_permissions():
    manifest_path = Path(__file__).resolve().parent.parent / "agents_config.json"
    manifest = load_manifest(manifest_path)
    enforcer = PoLPEnforcer(manifest)

    # Order Poller permissions
    assert enforcer.can_read("order_poller", "storage/stickers/12345.json") is True
    assert enforcer.can_write("order_poller", "storage/stickers/12345.json") is True
    assert enforcer.can_delete("order_poller", "storage/stickers/12345.json") is False

    # Cleanup Agent delete permissions
    assert enforcer.can_delete("cleanup", "storage/temp/cache.tmp") is True
    assert enforcer.can_delete("cleanup", "storage/cz_docs/signed.xml") is False

    # Notifier Agent DB permissions
    assert enforcer.get_table_permission("notifier", "orders") == "SELECT"
    assert enforcer.get_table_permission("order_poller", "orders") == "SELECT_INSERT_UPDATE"
    assert enforcer.get_table_permission("cleanup", "audit_logs") == "DELETE"