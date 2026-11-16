"""Guards for the memory budget of the production processes (957 MB host)."""
import subprocess
import sys

from sqlalchemy import inspect as sa_inspect


def _loaded_in_fresh_process(setup: str, modules: list[str]) -> dict[str, bool]:
    code = f"import sys\n{setup}\nprint(','.join(str(m in sys.modules) for m in {modules!r}))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    flags = out.stdout.strip().splitlines()[-1].split(",")
    return {m: flag == "True" for m, flag in zip(modules, flags)}


def test_worker_does_not_load_bot_or_web_frameworks():
    """aiogram costs ~50 MB per process; the worker only needs to send messages."""
    setup = (
        "sys.argv = ['celery', 'worker']\n"
        "import importlib, app.celery_app as ca\n"
        "[importlib.import_module(m) for m in ca._AGENT_MODULES]"
    )
    loaded = _loaded_in_fresh_process(setup, ["aiogram", "fastapi", "openpyxl", "numpy"])
    assert loaded == {"aiogram": False, "fastapi": False, "openpyxl": False, "numpy": False}


def test_api_does_not_load_aiogram():
    assert _loaded_in_fresh_process("import app.main", ["aiogram"]) == {"aiogram": False}


def test_bot_does_not_load_web_framework_or_excel():
    loaded = _loaded_in_fresh_process("import run_bot", ["fastapi", "openpyxl", "numpy"])
    assert loaded == {"fastapi": False, "openpyxl": False, "numpy": False}


def test_agents_share_one_sync_engine():
    """Each engine keeps its own idle PostgreSQL connections, and each connection is a server process."""
    from app.agents import archive_processor, cz_withdrawal, notifier, order_poller, wb_finance_agent
    from app.bot import helpers
    from app.database_sync import sync_engine

    for module in (archive_processor, cz_withdrawal, notifier, order_poller, wb_finance_agent, helpers):
        assert module.sync_engine is sync_engine, module.__name__


def test_sync_engine_pool_is_small_for_postgres():
    from app.database_sync import build_sync_engine_kwargs

    kwargs = build_sync_engine_kwargs("postgresql+psycopg2://u:p@postgres/db")
    assert kwargs["pool_size"] <= 2
    assert kwargs["pool_pre_ping"] is True
    assert "pool_size" not in build_sync_engine_kwargs("sqlite:///./wbfbs.db")


def test_sales_report_raw_payload_is_not_loaded_by_default():
    from app.models.wb_finance import WbSalesReportRow

    assert sa_inspect(WbSalesReportRow).attrs["raw_payload"].deferred is True


def test_national_catalog_router_is_still_reachable():
    from app.national_catalog import nk_router
    from app.national_catalog.router import router

    assert nk_router is router
