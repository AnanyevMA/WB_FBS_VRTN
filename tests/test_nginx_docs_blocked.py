"""
API docs must be closed from the internet by nginx, while the application itself
keeps serving them for local development and from inside the api container.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

APP_CONF = Path("nginx/conf.d/app.conf")


def _blocked_location_pattern() -> str:
    conf = APP_CONF.read_text(encoding="utf-8")
    match = re.search(r"location ~ (\S*docs\S*) \{\s*return 404;\s*\}", conf)
    assert match, "nginx config has no 404 location for the API docs"
    return match.group(1)


@pytest.mark.parametrize("path", ["/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"])
def test_nginx_blocks_docs_paths(path):
    assert re.search(_blocked_location_pattern(), path)


@pytest.mark.parametrize("path", [
    "/", "/health", "/static/index.html", "/api/v1/sellers",
    "/vacation/docs", "/docs.html", "/redocs", "/api/v1/docs",
])
def test_nginx_keeps_other_paths_open(path):
    assert not re.search(_blocked_location_pattern(), path)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_application_still_serves_docs(path):
    """The block lives in nginx only, so the docs stay usable while working on the project."""
    assert TestClient(app).get(path).status_code == 200
