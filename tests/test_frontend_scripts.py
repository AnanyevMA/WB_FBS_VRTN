"""
Frontend scripts share one global scope, so a repeated top-level let/const stops the whole
second file from loading. That is how kiz.js silently stopped working for two months.
"""
import re
from pathlib import Path

from app.api.kiz import router as kiz_router

FRONTEND = Path("frontend")
SELLER_PREFIX = "/sellers/{seller_id}"
TOP_LEVEL_DECLARATION = re.compile(r"^(?:let|const|class)\s+([A-Za-z_$][\w$]*)", re.M)
REMOVED_HANDLERS = (
    "processKizScan", "triggerCzAction", "openKizSigningModal", "openBatchKizSigningModal",
    "executeKizClientSigning", "submitKizViaServerFallback", "currentSigningPayload", "onCertSelected",
)


def _html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


def _loaded_scripts() -> list[Path]:
    sources = re.findall(r'<script[^>]+src="(js/[^"?]+)', _html())
    assert sources, "index.html loads no local scripts"
    return [FRONTEND / src for src in sources]


def _page(html: str, route: str) -> str:
    start = html.index(f'id="page-{route}"')
    end = html.find('id="page-', start + 1)
    return html[start:end if end > 0 else len(html)]


def test_scripts_do_not_redeclare_top_level_names():
    seen: dict[str, str] = {}
    duplicates = []
    for path in _loaded_scripts():
        for name in TOP_LEVEL_DECLARATION.findall(path.read_text(encoding="utf-8")):
            if name in seen:
                duplicates.append(f"{name}: {seen[name]} and {path.name}")
            seen[name] = path.name
    assert duplicates == []


def test_every_loaded_script_exists_and_is_versioned():
    html = _html()
    for path in _loaded_scripts():
        assert path.exists(), path
        if path.name != "cadesplugin_api.js":
            assert re.search(rf'js/{re.escape(path.name)}\?v=', html), path.name


def test_scanner_page_is_removed():
    html = _html()
    assert 'page-/kiz' not in html and 'data-route="/kiz"' not in html
    assert "kizScannerInput" not in html


def test_manual_order_signing_is_removed():
    assert not (FRONTEND / "js/kiz.js").exists()
    html = _html()
    assert 'id="kizSigningModal"' not in html
    assert "kizSigning" not in html
    for name in REMOVED_HANDLERS:
        assert name not in html, name
        for path in _loaded_scripts():
            assert name not in path.read_text(encoding="utf-8"), f"{name} in {path.name}"


def _kiz_paths() -> set[str]:
    return {route.path for route in kiz_router.routes}


def test_manual_signing_routes_are_removed():
    assert f"{SELLER_PREFIX}/orders/{{order_id}}/kiz" in _kiz_paths(), "route table is not the expected one"
    for route in ("withdraw", "return", "prepare-document", "submit-signed-document", "attach"):
        assert f"{SELLER_PREFIX}/kiz/{route}" not in _kiz_paths(), route


def test_signature_queue_keeps_its_signing_tools():
    """Signing now lives only in the queue, which needs the certificate select and archive upload."""
    assert 'batchCertSelect' in (FRONTEND / "js/signature_queue.js").read_text(encoding="utf-8")
    assert (FRONTEND / "js/cryptopro.js").exists()
    assert f"{SELLER_PREFIX}/kiz/signature-batches/{{batch_id}}/prepare-documents" in _kiz_paths()
    assert f"{SELLER_PREFIX}/kiz/signature-batches/{{batch_id}}/submit-signed" in _kiz_paths()


def test_archive_upload_lives_on_the_signature_queue_page():
    """openArchiveFileInput() needs this input: it used to live only on the removed page."""
    html = _html()
    queue = _page(html, "/signature-queue")
    assert html.count('id="archiveFileInput"') == 1
    for element in ('id="archiveFileInput"', 'id="archiveDropZone"', 'id="archiveApiSyncBtn"'):
        assert element in queue, element
