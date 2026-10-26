# Project: WB FBS Manager — Item Returns & Excise Report Integration

## Architecture
- **WB Analytics Integration**: `WBAnalyticsClient` (`app/services/wb_analytics_client.py`) connects to `https://seller-analytics-api.wildberries.ru/api/analytics/v1/item-returns` with 429 rate limit backoff and tenacity retries.
- **Returns Domain Service**: `wb_item_returns_service.py` matches returns by `srid` / `orderId` / `nmId` against `Order` and `KizProductInfo`.
  - Bifurcation logic:
    - If True API status is `RETIRED`: generates `KizSignatureBatch` return items for `LP_RETURN`.
    - If True API status is `INTRODUCED`: cancels planned withdrawal, transitions `Order.kiz_status` to `RETURNED`, and preserves local stock without extraneous ГИС МТ documents.
- **Excise Report Return Processing**: `wb_warehouse_sales_service.py` (with `wb_warehouse_sales_builder.py` helper to preserve <400 lines limit) captures storno/return receipts, validates True API status, and appends to `KizSignatureBatch` returns.
- **Background Orchestration**: `wb_item_returns_agent.py` running in Celery queue `cz_operations` on Beat cadence (300s polling, per-seller 6h interval) with strict PoLP matrix and `AuditLog` records.
- **API & UI Layer**: `app/api/kiz/item_returns.py` router mounted under `/sellers/{seller_id}`, `Seller` model/schema extensions (`item_returns_enabled`, `item_returns_interval_hours`), and synchronous UI integration in `frontend/index.html` and `frontend/js/sellers.js`.
- **Downstream Signing & Verification**: Submissions use existing `signature_batch_submitter.py` and `cz_client.py` without expanding legacy files.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Baseline Test & Invariant Verification | Run baseline pytest, verify Order/Kiz/Batch/Finance schema contracts and test isolation | M1 | ORIGINAL_REQUEST § R1 |
| 2 | WB Analytics `get_item_returns` | Implement HTTP client method for `/api/analytics/v1/item-returns` with 429 backoff & retries | M1 | ORIGINAL_REQUEST § R2 |
| 3 | Returns Domain Service & Bifurcation | Match returns, check True API: RETIRED -> LP_RETURN batch, INTRODUCED -> cancel withdrawal & set RETURNED | M1 | ORIGINAL_REQUEST § R2 |
| 4 | Warehouse Sales Builder Extraction | Extract item building logic to `wb_warehouse_sales_builder.py` to preserve <400 line limit | M2 | PROJECT_RULES & Survey 2 |
| 5 | Excise Report Return Integration | In `wb_warehouse_sales_service.py`, parse storno/return receipts and append to `KizSignatureBatch` returns | M2 | ORIGINAL_REQUEST § R3 |
| 6 | Celery Agent `wb_item_returns_agent` | Create periodic agent in `cz_operations` queue checking configured seller interval | M3 | ORIGINAL_REQUEST § R4 |
| 7 | Celery Registration & PoLP Matrix | Register agent in `celery_app.py` and `agents_config.json` with strict PoLP & AuditLog | M3 | ORIGINAL_REQUEST § R4 |
| 8 | Seller Model & Schema Settings | Add `item_returns_enabled` and `item_returns_interval_hours` to `Seller` model and Pydantic schemas | M4 | ORIGINAL_REQUEST § R5 |
| 9 | REST API Kiz Item Returns Router | Create `app/api/kiz/item_returns.py` with manual sync (`POST`) and history (`GET`) endpoints | M4 | ORIGINAL_REQUEST § R5 |
| 10 | Frontend Settings & Badge Coverage | Add modal settings card in `index.html`, update `editSeller`/`saveSeller` in `sellers.js`, verify `RETURNED` status badges | M4 | ORIGINAL_REQUEST § R5 |
| 11 | Comprehensive E2E Testing Suite | Requirements-driven test suite (Tiers 1-4) covering all features and corner cases | E2E Track | ORIGINAL_REQUEST § AC |
| 12 | Final Integrity Verification & Codebase Indexing | Full test run, verify data invariants, execute `CodebaseIndexer().save_index()` | M5 | ORIGINAL_REQUEST § R6 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Core Returns Engine & Invariants | Baseline audit, `wb_analytics_client.py` (`get_item_returns`), and `wb_item_returns_service.py` (bifurcation logic) | None | DONE |
| M2 | Excise Report Return Processing | Extract `wb_warehouse_sales_builder.py`, update `wb_warehouse_sales_service.py` to process storno/returns for LP_RETURN | M1 | DONE |
| M3 | Celery Background Agent & PoLP | `wb_item_returns_agent.py`, `celery_app.py` routing & beat schedule, `agents_config.json` PoLP matrix & `AuditLog` | M1 | DONE |
| M4 | REST API, Seller Settings & Frontend | `app/api/kiz/item_returns.py`, `Seller` fields/schemas, `index.html`, `sellers.js` | M1 | DONE |
| M5 | Final Dual-Track Pass & Indexing | 100% test pass of all tiers, invariant check, refresh `codebase_index.json` & `CODEBASE_MAP.md` | M1, M2, M3, M4, E2E | DONE |

## Interface Contracts
### WB Analytics Client ↔ Returns Service
- `WBAnalyticsClient.get_item_returns(date_from: datetime, date_to: datetime, nm_ids: Optional[List[int]] = None, limit: int = 1000, offset: int = 0) -> List[Dict[str, Any]]`
- Returns list of return dicts containing: `srid`, `orderId`, `nmId`, `barcodes`, `date`, `reason`.

### Returns Service ↔ True API & Signature Batch
- `wb_item_returns_service.sync_seller_returns(db: Session, seller_id: int, date_from: datetime, date_to: datetime) -> Dict[str, Any]`
  - For `RETIRED` KIZs: appends to `batch_returns` -> `KizSignatureBatch(sales_count=0, returns_count=N, data_payload={"returns": [...]})`.
  - For `INTRODUCED` KIZs: sets `Order.kiz_status = KizStatus.RETURNED`, removes/cancels planned withdrawal, updates `KizProductInfo`.

### Warehouse Sales Builder ↔ Warehouse Sales Service
- `build_warehouse_sale_item(...) -> Dict[str, Any]`
- `build_warehouse_return_item(...) -> Dict[str, Any]`

### Celery Agent ↔ Celery App
- Task: `app.agents.wb_item_returns_agent.check_and_sync_item_returns`
- Queue: `cz_operations`
- AuditLog: records `event_type="ITEM_RETURNS_SYNC"` with status `SUCCESS` or `ERROR`.

### REST API ↔ Frontend
- `POST /sellers/{seller_id}/item-returns/sync?date_from=...&date_to=...` -> `{"success": true, "processed_count": int, "batches_created": int}`
- `GET /sellers/{seller_id}/item-returns/history` -> list of return reconciliation events / batches.

## Code Layout
- `app/services/wb_analytics_client.py` (current 105 lines, target < 160 lines)
- `app/services/wb_item_returns_service.py` (new module, target < 280 lines)
- `app/services/wb_warehouse_sales_service.py` (current 372 lines, target < 350 lines with builder extraction)
- `app/services/wb_warehouse_sales_builder.py` (new module, target < 120 lines)
- `app/agents/wb_item_returns_agent.py` (new module, target < 150 lines)
- `app/celery_app.py` (current 378 lines, target < 395 lines)
- `app/models/seller.py` (current 127 lines, target < 145 lines)
- `app/schemas/seller.py` (current 376 lines, target < 395 lines)
- `app/api/kiz/item_returns.py` (new module, target < 100 lines)
- `app/api/kiz/__init__.py` (mounting router)
- `frontend/index.html` (seller modal card)
- `frontend/js/sellers.js` (seller modal population & saving)
- `agents_config.json` (PoLP matrix)
