# E2E Test Infra: WB FBS Manager Item Returns & Excise Report Integration

## Test Philosophy
- **Opaque-box & Requirement-driven**: Derived directly from `ORIGINAL_REQUEST.md` and user-facing specifications, independent of internal implementation details.
- **Methodology**: 4-Tier test engineering:
  - Tier 1: Feature Coverage (representative equivalence class tests per feature)
  - Tier 2: Boundary & Corner Cases (empty responses, 429 rate limit backoff, missing KIZs, missing SRIDs, token expiration, line limit enforcement)
  - Tier 3: Cross-Feature Combinations (Returns + Excise Report interactions, Celery Beat interval + manual REST trigger, Seller update with encrypted tokens preservation)
  - Tier 4: Real-World Application Scenarios (Customer return after delivery -> RETIRED -> LP_RETURN; Customer cancellation before delivery -> INTRODUCED -> RETURNED without ГИС МТ document; Excise report storno handling)

## Feature Inventory
| # | Feature | Source | Tier 1 | Tier 2 | Tier 3 |
|---|---------|--------|:------:|:------:|:------:|
| 1 | Baseline & Data Model Invariants | ORIGINAL_REQUEST § R1 | 5 | 5 | ✓ |
| 2 | WB Analytics `get_item_returns` | ORIGINAL_REQUEST § R2 | 5 | 5 | ✓ |
| 3 | Returns Domain Service & Bifurcation | ORIGINAL_REQUEST § R2 | 5 | 5 | ✓ |
| 4 | Excise Report Return Enhancement | ORIGINAL_REQUEST § R3 | 5 | 5 | ✓ |
| 5 | Celery Agent & PoLP Security | ORIGINAL_REQUEST § R4 | 5 | 5 | ✓ |
| 6 | REST API & Frontend Coverage | ORIGINAL_REQUEST § R5 | 5 | 5 | ✓ |

## Test Architecture
- **Test Runner**: `.venv\Scripts\pytest`
- **Test Files Location**: `tests/`
  - `tests/test_wb_item_returns_service.py` (Unit & service tests for item returns)
  - `tests/test_wb_analytics_client_returns.py` (Client tests with mocked HTTP responses & 429 retries)
  - `tests/test_warehouse_sales_storno_returns.py` (Excise report return & storno processing)
  - `tests/test_item_returns_agent_celery.py` (Celery task, beat schedule, PoLP enforcement, and AuditLog)
  - `tests/test_api_item_returns_and_frontend.py` (REST endpoints, seller settings schemas, and frontend consistency)
  - `tests/test_e2e_item_returns_lifecycle.py` (End-to-end integration and dual-track scenarios)

## Real-World Application Scenarios (Tier 4)
| # | Scenario | Features Exercised | Complexity |
|---|----------|--------------------|------------|
| 1 | Post-Sale Customer Return (RETIRED -> LP_RETURN) | Client, Service, True API, Batch Submitter | High |
| 2 | Pre-Sale Customer Refusal (INTRODUCED -> RETURNED) | Client, Service, True API, Inventory Reversal | Medium |
| 3 | Excise Report Storno with Fiscal Return Receipt | Warehouse Sales, Storno Processing, Batch Submitter | High |
| 4 | Concurrent Polling & Manual Sync with 429 Throttling | Client 429 Backoff, Agent Celery, REST API | High |
| 5 | Seller Config Update & Frontend Roundtrip | Seller Model, Schemas, HTML/JS, Token Preservation | Medium |

## Coverage Thresholds
- Tier 1: ≥5 tests per feature area (total ≥ 30 tests)
- Tier 2: ≥5 boundary tests per feature area (total ≥ 30 tests)
- Tier 3: Pairwise combinations across major features (≥ 10 tests)
- Tier 4: Real-world application scenarios (≥ 5 comprehensive integration tests)
- Total E2E Target: ≥ 75 new verified test cases without any regression of baseline suites.
