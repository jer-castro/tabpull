---
name: verify-tabpull
description: Drive the tabpull CLI (tabpull add/run/login/setup) against the real Tableau site and prove exports, filters, date ranges and view search with captured evidence. Use before shipping any change to src/tabpull/crosstab.py or src/tabpull/tableau.py, or when a filter, date or search behavior is in question.
---

# Verify tabpull

The app is a short-lived CLI. There is no server to start: every drive is one `uv run tabpull ...` process against the live Tableau sites saved by `tabpull setup`. Work from the repo root. Tokens, SSO cookies, and the jobs file live in the XDG config directory (or the OS equivalent), described in the README, and do not follow the working directory. Exports go to the working directory unless `--out` is passed. `TABLEAU_*` environment variables and a `.env` in the repo are not read.

## Launch

```sh
cd <repo root>
uv sync                                   # once
RUN=$(date +%Y%m%d-%H%M%S); E=.verify/$RUN; mkdir -p $E
```

Every drive writes to `$E`, never to the config `jobs.toml` or the working directory (the default `--out`): pass `--jobs $E/<name>/jobs.toml --out $E/<name>/exports`. `.verify/` is gitignored. Forgetting `--jobs` edits the real jobs file.

Leave `XDG_CONFIG_HOME` and `XDG_DATA_HOME` unset when the drive should use the real site tokens and SSO cookies. Set both to a directory inside `$E` only for the negative session tests, so those tests cannot touch the real cookies. Those drives call `.venv/bin/tabpull` (or `.venv/bin/python`), never `uv run`: uv keeps its managed Python under `XDG_DATA_HOME`, so `uv run` with that variable set downloads a new interpreter and rebuilds the repo `.venv` against the scratch directory. If that happens, run `uv sync` with both variables unset.

## Doctor

```sh
uv run python .agents/skills/verify-tabpull/scripts/verify.py doctor
```

It prints one `settings` and one `session` line per configured site, then a `jobs` line, and exits 0 only when all of them are `ok`. It prints the local site name, the server, and the Tableau site, never the token. `settings FAIL` stops that site: no site is configured, or that site's file is missing keys, and only a human can fix it with `tabpull setup` (it asks for the token secret). `session FAIL` means `<config>/tabpull/auth/<name>.json` (the path on the `session` line) is missing or its SSO cookies expired. Only a human can fix that, because SSO/MFA runs in a visible browser: ask them to run `tabpull login --site <name>`, then rerun doctor. Don't drive anything while doctor fails.

## Drive

- **CLI prompts.** Interactive `add` reads plain `input()`. Pipe the answers in order: `printf 'answer1\nanswer2\n...' | uv run tabpull --jobs $E/add/jobs.toml add --site <name>`. `--site` belongs to `add`, `run` has no `--site`, and `tabpull --site ...` fails with `invalid choice`. Always pass it in piped drives: with more than one configured site and no `--site`, piped `add` exits with `Pass --site. Configured sites: ...`. No PTY is needed while the SSO session is valid. A non-tty `run` with an expired session fails each job with `Run: tabpull login --site <name>` instead of opening a window.
- **Flags.** `tabpull add --site <name> --view Workbook/View --sheet "Sheet" --filter 'Field=a|b'` writes a job and does not prompt. It opens the view only to refuse a story (`Stories are not supported; use the dashboard inside it.`) and does not list sheets or export. A filter with no ` @Sheet` prints that it is applied on the first `--sheet`.
- **View structure.** `verify.py --site <name> inspect Workbook/View` prints the sheets (hidden ones too), each sheet's filters with their type and current value, and the parameters, as JSON. Use it to pick real sheet and field names before writing a scratch job. `--site` may be omitted when only one site is configured. Put `--site` before the subcommand.
- **Date ranges.** `verify.py --site <name> date-filter Workbook/View --sheet S --field F --min YYYY-MM-DD --max YYYY-MM-DD --out $E/date` applies the range the way `run` does (same `APPLY_JS`, same `export_embed`) in browsers set to UTC, America/Los_Angeles and Pacific/Auckland. It passes only if Tableau reports the requested days in every timezone and the three crosstab CSVs are byte-identical.
- **Feature recipes** live in [features/README.md](features/README.md). Pick the feature you changed and drive every entry point it lists.

Known-good workbook and sheet names are listed below. The pod, the Tableau site, and the view URL live in `local/site.md`, which is gitignored. Copy [local/site.md.example](local/site.md.example) if that file is missing, and ask for the sandbox values instead of guessing. `<site>` below means the local name from `tabpull setup`, which doctor prints on the `settings ok` line. It can match the Tableau site content URL, but it is the local name.

- `CrosstabMe/Dashboard1`: a dashboard with sheets `A Title Sheet` (no filters) and `B Real Sheet`. `B Real Sheet` has categorical `Measure Names`, range `Order Date` 1/3/2023..12/30/2026, and categorical `Ship Mode`. The dashboard has parameters `Top Customers` and `Profit Bin Size`, and neither changes `B Real Sheet`.
- `Superstore/Performance`: a published worksheet, exported via the browser like any other view.

## Evidence

Everything goes under `.verify/<run-id>/`: command logs (`2>&1 | tee $E/<name>/run.log` plus a trailing `EXIT:<code>` line), scratch `jobs.toml`, exported CSVs, and `date-filter.json`.

- Drive the real CLI path. The only exception is `verify.py`, which calls the same functions `run` does.
- Prove side effects: the CSV exists at `<out>/<job>/<sheet>.csv` (both names slugged: spaces become `_`), plus its line count or `sha256`. Don't open or paste exported rows. They're the user's data, so line counts, hashes and header rows are the limit.
- A filter proof needs a control: the same export with the filter changed or removed must differ. Identical output under a changed filter means the filter did nothing, or the chosen day has no rows.
- `add` proof is the log plus the saved scratch `jobs.toml`.

## Cleanup

There's nothing to stop: each CLI and `verify.py` call closes its own browser. Remove nothing under `.verify/<run-id>/`, since that's the proof. If an interrupted run left a headless Chrome behind, kill only the PID you started (`pgrep -f -n 'playwright'` right after your own launch), never by process name.

## Isolation and gotchas

- Parallel browser drives are safe: each one opens its own context from that site's auth file, read-only.
- Don't run `add` drives in parallel when they sign in with the PAT to list views. Signing in with the PAT ends any other session on the same PAT, and that includes the user's own scripts. Flag `add` does not sign in.
- `tabpull login` and `tabpull setup` need a human (a visible browser, and a hidden questionary prompt for the secret). A non-tty `setup` exits with `tabpull setup needs a terminal`. Report them as unverified rather than faking a tty.
