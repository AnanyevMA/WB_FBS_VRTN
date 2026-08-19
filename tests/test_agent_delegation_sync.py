"""
Integration test for Agent Task Delegation, Audit Logging, and Workflow Synchronization.
"""
from pathlib import Path
from app.agent_manifest import load_manifest
from app.celery_app import celery_app


def test_agent_task_registration_and_queues():
    celery_app.loader.import_default_modules()
    manifest_path = Path(__file__).resolve().parent.parent / "agents_config.json"
    manifest = load_manifest(manifest_path)

    registered_tasks = celery_app.tasks.keys()
    task_routes = celery_app.conf.task_routes

    for agent in manifest.agents:
        # Check task registered in Celery
        assert agent.celery_task in registered_tasks, f"Task {agent.celery_task} not registered in Celery!"

        # Check queue routing
        route_found = False
        for route_pattern, route_config in task_routes.items():
            pattern_prefix = route_pattern.rstrip("*").rstrip(".")
            if agent.celery_task.startswith(pattern_prefix):
                assert route_config["queue"] == agent.queue, (
                    f"Agent {agent.id} queue mismatch! Manifest says {agent.queue}, route says {route_config['queue']}"
                )
                route_found = True
                break

        assert route_found, f"No Celery route found for task {agent.celery_task}"


def test_celery_beat_schedule_synchronization():
    beat_schedule = celery_app.conf.beat_schedule
    scheduled_tasks = [item["task"] for item in beat_schedule.values()]

    expected_scheduled = [
        "app.agents.order_poller.poll_all_sellers",
        "app.agents.cz_token_refresher.refresh_all_tokens",
        "app.agents.archive_processor.process_all_archives",
        "app.agents.cleanup.cleanup_old_audit_logs",
        "app.agents.qa_test_agent.run_system_regression_tests",
        "app.agents.morning_digest.send_morning_digest",
    ]

    for task_name in expected_scheduled:
        assert task_name in scheduled_tasks, f"Task {task_name} missing from Celery Beat schedule!"


def test_qa_agent_execution_and_audit_logging():
    from app.agents.qa_test_agent import run_system_regression_tests

    result = run_system_regression_tests()
    assert isinstance(result, dict)
    assert result["passed"] >= 4
    assert result["all_passed"] is True
