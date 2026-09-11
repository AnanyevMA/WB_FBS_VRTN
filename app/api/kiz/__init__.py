"""
FastAPI KIZ Router Package — WB FBS Manager
Маркировка Честный Знак, сканирование, True API и подписание документов ЭЦП.
"""
from fastapi import APIRouter
from app.api.kiz.attach import (
    router as attach_router,
    attach_kiz,
    lookup_kiz,
    detach_kiz,
    validate_kiz,
    list_kiz_operations,
)
from app.api.kiz.documents import (
    router as documents_router,
    withdraw_kiz,
    return_kiz,
    prepare_kiz_document,
    submit_signed_kiz_document,
)
from app.api.kiz.archive import (
    router as archive_router,
    preview_wb_archive,
    sync_archive_kiz_with_cz,
    process_wb_archive,
)
from app.api.kiz.signature_batches import (
    router as signature_batches_router,
    list_signature_batches,
    get_signature_batch,
    prepare_batch_documents_for_signing,
    submit_signed_batch,
    cancel_signature_batch,
)
from app.api.kiz.auto_queue import (
    router as auto_queue_router,
    trigger_auto_kiz_batch_now,
    get_auto_kiz_settings,
    update_auto_kiz_settings,
)

router = APIRouter(prefix="/sellers/{seller_id}", tags=["kiz"])

# Include sub-routers
router.include_router(attach_router)
router.include_router(documents_router)
router.include_router(archive_router)
router.include_router(signature_batches_router)
router.include_router(auto_queue_router)

__all__ = [
    "router",
    "attach_kiz",
    "lookup_kiz",
    "detach_kiz",
    "validate_kiz",
    "list_kiz_operations",
    "withdraw_kiz",
    "return_kiz",
    "prepare_kiz_document",
    "submit_signed_kiz_document",
    "preview_wb_archive",
    "sync_archive_kiz_with_cz",
    "process_wb_archive",
    "list_signature_batches",
    "get_signature_batch",
    "prepare_batch_documents_for_signing",
    "submit_signed_batch",
    "cancel_signature_batch",
    "trigger_auto_kiz_batch_now",
    "get_auto_kiz_settings",
    "update_auto_kiz_settings",
]
