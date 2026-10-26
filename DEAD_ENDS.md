# Dead Ends Log

| Iteration | Approach Tried | Why It Failed | Files Touched |
|-----------|---------------|---------------|---------------|
| Survey 1 | Monolithic pytest without test database isolation | SQLite database wbfbs.db gets locked/polluted across test files causing 40 flaky failures | None (read-only) |
| Survey 2 | Directly inlining return generation inside `wb_warehouse_sales_service.py` | Exceeds PROJECT_RULES.md hard ceiling of 400 lines (file is 372 lines) | None (read-only) |
| Survey 2 | Modifying `cz_client.py` for returns | Unnecessary: `_build_return_document`, `build_return_payload`, and `return_to_circulation` already exist and are fully functional | None (read-only) |
