---
name: verify-tabpull
description: Drive the tabpull CLI (src/crosstab.py add/run/login) against the real Tableau site and prove exports, filters, date ranges, REST value escaping and view search with captured evidence. Use before shipping any change to crosstab.py or tableau.py, or when a filter, date or search behavior is in question.
---

# Verify tabpull

The app is a short-lived CLI. There is no server to start: every drive is one `uv run src/crosstab.py ...` process against the live Tableau site in `.env`. Work from the repo root; `.env`, `.auth/tableau-state.json` and `jobs.toml` resolve relative to the current directory.

## Launch

```sh
cd <repo root>
uv sync                                   # once
RUN=$(date +%Y%m%d-%H%M%S); E=.verify/$RUN; mkdir -p $E
```

Every drive writes to `$E`, never to `jobs.toml` or `exports/`: pass `--jobs $E/<name>/jobs.toml --out $E/<name>/exports`. `.verify/` is gitignored.

## Doctor

```sh
uv run python .agents/skills/verify-tabpull/scripts/verify.py doctor
```

It prints `settings`, `jobs.toml` and `session` lines and exits 0 only when all three are `ok`. It prints the server and site, never the PAT. `settings FAIL` stops there: `.env` (or the `TABLEAU_*` variables) is missing keys, and only a human can fix that with `uv run src/wizard.py`, since it asks for the PAT secret. `session FAIL` means `.auth/tableau-state.json` is missing or its SSO cookies expired. Only a human can fix that, because SSO/MFA runs in a visible browser: ask them to run `uv run src/crosstab.py login`, then rerun doctor. Don't drive anything while doctor fails.

## Drive

- **CLI prompts.** `add` reads plain `input()`, so pipe the answers in order: `printf 'answer1\nanswer2\n...' | uv run src/crosstab.py --jobs $E/add/jobs.toml add`. No PTY is needed while the SSO session is valid. A non-tty `run` with an expired session exits with "Run: uv run src/crosstab.py login" instead of opening a window.
- **View structure.** `verify.py inspect Workbook/View` prints the sheets (hidden ones too), each sheet's filters with their type and current value, and the parameters, as JSON. Use it to pick real sheet and field names before writing a scratch job.
- **Date ranges.** `verify.py date-filter Workbook/View --sheet S --field F --min YYYY-MM-DD --max YYYY-MM-DD --out $E/date` applies the range the way `run` does (same `APPLY_JS`, same `export_embed`) in browsers set to UTC, America/Los_Angeles and Pacific/Auckland. It passes only if Tableau reports the requested days in every timezone and the three crosstab CSVs are byte-identical.
- **Feature recipes** live in [features/README.md](features/README.md). Pick the feature you changed and drive every entry point it lists.

Known-good fixtures on the current test site (`10ax.online.tableau.com`, site `jercastro`), checked live on 2026-09-26:

- `CrosstabMe/Dashboard1`: a dashboard with sheets `A Title Sheet` (no filters) and `B Real Sheet`. `B Real Sheet` has categorical `Measure Names`, range `Order Date` 1/3/2023..12/30/2026, and categorical `Ship Mode`. The dashboard has parameters `Top Customers` and `Profit Bin Size`, and neither changes `B Real Sheet`.
- `Superstore/OrderDetails`: a dashboard, view id `011b479c-65d3-43ca-a855-8ae63ffac15e`. Its REST export has `Customer Name, Measure Names, Order Date, Order ID, Ship Date, Ship Mode, Measure Values`, not `Product Name`.
- `WorldIndicators/Population`: a published worksheet, view id `7355995d-1ae5-4fa4-a677-7713d2fb9f76`. `Country/Region` has values with commas, which makes it the REST comma fixture.
- `Superstore/Performance`: a published worksheet (REST `sheetType` `view`), so `add` offers REST for it.

## Evidence

Everything goes under `.verify/<run-id>/`: command logs (`2>&1 | tee $E/<name>/run.log` plus a trailing `EXIT:<code>` line), scratch `jobs.toml`, exported CSVs, and `date-filter.json`.

- Drive the real CLI path. The only exception is `verify.py`, which calls the same functions `run` does.
- Prove side effects: the CSV exists at `exports/<job>/<sheet>.csv` (both names slugged: spaces become `_`), plus its line count or `sha256`. Don't open or paste exported rows. They're the user's data, so line counts, hashes and header rows are the limit.
- A filter proof needs a control: the same export with the filter changed or removed must differ. Identical output under a changed filter means the filter did nothing, or the chosen day has no rows.
- `add` proof is the log plus the saved scratch `jobs.toml`.

## Cleanup

There's nothing to stop: each CLI and `verify.py` call closes its own browser. Remove nothing under `.verify/<run-id>/`, since that's the proof. If an interrupted run left a headless Chrome behind, kill only the PID you started (`pgrep -f -n 'playwright'` right after your own launch), never by process name.

## Isolation and gotchas

- Parallel browser drives are safe: each one opens its own context from `.auth/tableau-state.json` read-only.
- Don't run REST drives in parallel. That includes `add`, which signs in with the PAT to list views. Signing in with the PAT ends any other session on the same PAT, and that includes the user's own scripts.
- REST exports use `maxAge=1`, so Tableau may serve data up to a minute old.
- `login` and `src/wizard.py` need a human (a visible browser, and `getpass` for the secret). Report them as unverified rather than faking a tty.
