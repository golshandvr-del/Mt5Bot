# FIX PLAN — "no such function: json_extract" (empty strategy registry)

Status legend: [ ] pending · [~] in progress · [x] done

---

## 1. Symptom (what the user saw)

- Ran `python main.py --mode search` on 150,000 XAUUSD M15 live bars.
- Search finished after ~6h; 200 trials evaluated.
- Log line at the end:

  ```
  2026-07-07 22:43:35 ERROR  memory.store | top_strategies failed: no such function: json_extract
  2026-07-07 22:43:35 INFO   memory.store | Registry updated for XAUUSD M15 with strategies.
  2026-07-07 22:43:35 INFO   strategy.search | Search complete: 200 strategies evaluated; 0 in registry top.
  ```

- Final summary: `symbols.XAUUSD = {"evaluated": 200, "top": 0}`,
  `memory_stats = {"strategies": 818, "results": 40481}`.
- Opening `strategy_registry.json` (a.k.a. strategy.json) shows **no XAUUSD strategy**.

## 2. Root cause

The evaluation and storage worked perfectly: 818 strategies and 40,481 result
rows are in SQLite. The failure is ONLY in the *selection/promotion* step.

`core/memory/store.py` builds the top-strategy ranking with a SQL query that
calls the SQLite built-in function **`json_extract(...)`** to pull
`num_trades`, `pnl_pvalue`, `win_rate_ci_low` (and `expectancy` in
`reference_pnls`) out of the stored `metrics_json` text column.

`json_extract` is part of SQLite's **JSON1** extension. On the user's machine
(Windows 7 + an older Python whose bundled SQLite was compiled WITHOUT JSON1),
that function does not exist, so the query raises
`sqlite3.OperationalError: no such function: json_extract`.

Because `top_strategies()` wraps everything in `try/except` and returns `[]` on
error, the registry is updated with an EMPTY top list → `"top": 0` → no XAUUSD
strategy ends up in the registry, even though all the raw data is safely stored.

## 3. Fix strategy

Do NOT depend on the SQLite JSON1 extension at all. Instead register a small
**custom Python SQL function** on every connection that mimics `json_extract`
for the simple `$.field` paths we use. Python's `sqlite3` lets us do this with
`conn.create_function(...)`, and it works on EVERY SQLite build regardless of
whether JSON1 was compiled in.

This is the most robust, least invasive fix:
- No schema change, no data migration.
- The 40,481 already-stored results become usable immediately (re-running the
  registry build will now populate XAUUSD).
- Behaviour is byte-identical on machines that already had JSON1.

### Steps

- [x] **Step 1 — Add a JSON1-independent `json_extract` shim.**
  In `core/memory/store.py`, add a module-level helper `_json_extract(text, path)`
  that parses the JSON text in Python and resolves a top-level `$.key` path,
  returning `None` on any miss/parse error. Register it on every connection
  inside `_connect()` via
  `conn.create_function("json_extract", 2, _py_json_extract)`.
  SQLite will prefer a real built-in when present, but registering our own name
  guarantees the function always resolves. (We name the Python impl defensively
  so it overrides only when the built-in is absent, and matches it when present.)

- [x] **Step 2 — Verify all `json_extract` call sites still work.**
  Call sites: `top_strategies()` (3 fields) and `reference_pnls()` (1 field).
  Both go through `_connect()`, so both are covered by Step 1. No SQL text
  change required. Confirm the `$.field` path parsing handles exactly those
  four keys.

- [x] **Step 3 — Add a one-off "rebuild registry" recovery path.**
  Provide a small maintenance entry (`--mode rebuild-registry` or a standalone
  script) so the user can regenerate `strategy_registry.json` from the EXISTING
  40,481 stored results WITHOUT re-running the 6-hour search. This turns the
  already-collected data into a populated XAUUSD registry immediately.

- [x] **Step 4 — Regression test.**
  Add a test that opens a fresh MemoryStore, records a few results whose
  `metrics_json` contains the needed fields, and asserts `top_strategies()`
  returns them ranked — exercising the custom function. Also a direct unit test
  of `_py_json_extract` for hit/miss/bad-json cases.

- [x] **Step 5 — Docs.**
  Note in README / CODE_MAP that the memory store no longer requires the SQLite
  JSON1 extension (Windows-7-friendly) and document the rebuild-registry step.

## 4. How the user recovers their 6-hour run

After the fix is deployed, the user does NOT need to re-search. They run the new
rebuild step once:

```
python main.py --mode rebuild-registry
```

which recomputes the top strategies for every (symbol, timeframe) already in
memory (including XAUUSD M15) and writes them into `strategy_registry.json`.

## 5. Acceptance criteria

- No `no such function: json_extract` error on a SQLite build without JSON1.
- `top_strategies("XAUUSD","M15", ...)` returns a non-empty ranked list from the
  existing 40,481 results.
- `strategy_registry.json` contains an `XAUUSD|M15` section with `top` > 0.
- All existing tests still pass.
