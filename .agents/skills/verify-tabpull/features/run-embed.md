# Embed export

`run` opens each job's view in a headless browser signed in as the site that job names, sets its parameters and filters, and writes one UTF-8 CSV per listed sheet to `<out>/<job>/<sheet>.csv` (both names slugged, so `B Real Sheet` becomes `B_Real_Sheet.csv`), the same content as Download > Crosstab > CSV. The default output folder is the data-directory `exports/`. `--out` replaces it for one run.

## Sub-features

- `embed-sheet` exports a named sheet, including hidden ones and ones that aren't first alphabetically.
- `embed-values` applies a categorical `values` filter on its `sheet`.
- `embed-range-date` applies a `YYYY-MM-DD` or `M/D/YYYY` `min`/`max` range as the same calendar days in any browser timezone, and rejects an impossible date such as `2/30/2024` or `2024-02-31`. A relative or run-time bound is refused.
- `embed-params` sets parameters before filters.
- `embed-failure` reports `✗ <job>: <reason>` for a broken job, keeps going, and exits 1.
- `embed-default-sheet` prints a note when a filter omits `sheet`, applies that filter on the first sheet in the job, and does not copy it onto the other sheets.
- `embed-site` loads the token and browser session for `job.site`. A missing site fails that job and run continues with the others.

## How to get to it (user POV)

- `tabpull run` exports every job in the jobs file.
- `tabpull run <name> ...` exports only the named jobs.

## Driving it with the CLI

Preconditions:

- Doctor is `ok`.
- `verify.py inspect CrosstabMe/Dashboard1` lists `B Real Sheet` with `Order Date` (range) and `Ship Mode` (categorical).

- **Values filter.** Write `$E/embed/jobs.toml` with a job on `site = "<site>"`, `view = "CrosstabMe/Dashboard1"`, `sheets = ["B Real Sheet"]`, `filters = [{ field = "Ship Mode", values = ["First Class"], sheet = "B Real Sheet" }]`, plus a second job with the same site and view, the same sheet, and no filters. Run `uv run tabpull --jobs $E/embed/jobs.toml --out $E/embed/exports run 2>&1 | tee $E/embed/run.log`. You should get two `✓` lines and exit 0, and the two CSVs' sha256 must differ.
- **Default sheet.** Repeat with the filter's `sheet` key removed and a second sheet in `sheets` (for example `A Title Sheet`). The log should contain `filter 'Ship Mode' names no sheet; applying it on 'B Real Sheet', the first sheet in the job.` The command should still export both sheets.
- **Date range across timezones.** Run `uv run python .agents/skills/verify-tabpull/scripts/verify.py --site <site> date-filter CrosstabMe/Dashboard1 --sheet "B Real Sheet" --field "Order Date" --min 2024-01-01 --max 2024-01-31 --out $E/date`. You should see `PASS`, a `min`/`max` `value` of `2024-01-01T00:00:00.000Z`/`2024-01-31T00:00:00.000Z` in all three timezone lines, the same `sha=` prefix on all three, and `evidence: $E/date/date-filter.json` (full hashes are in the JSON). `--site` may be omitted when only one site is configured.
- **M/D/YYYY bounds.** Rerun with `--min 1/1/2024 --max 1/31/2024 --out $E/date-us`. It should `PASS` with the same applied days and the same sha256 as the `YYYY-MM-DD` run.
- **Date control.** Rerun with `--max 2024-01-15 --out $E/date-control`. It should `PASS` again with a sha256 that differs from the first run.
- **Impossible date.** Add a job with `{ field = "Order Date", min = "2/30/2024", max = "3/1/2024" }` and run it. The jobs file loads fine; the export fails with `✗ <job>: '2/30/2024' is not a real date; use YYYY-MM-DD or M/D/YYYY`, and the run exits 1. A bad `YYYY-MM-DD` such as `2024-02-31` fails the same way, before the view opens. A relative bound such as `yesterday` fails with `is a relative date or a date computed at run time`.
- **Named job.** Run `run <second job name>` against the same jobs file. The log should show `Exporting 1 job(s)`.
- **Failure path.** Add a job with `sheets = ["No Such Sheet"]` and run it. You should get `✗ <job>: Page.evaluate: r: invalid-selection-sheet: sheetName parameter must belong to a worksheet within the current view`, the other jobs should still get `✓`, and the run should exit 1.
- **Parameters.** Add a job with `params = { "No Such Param" = "1" }`. It must fail with `✗ <job>: Page.evaluate: r: invalid-parameter: Invalid parameter:  No Such Param`, which proves `params` reach the view before any filter. A job with `params = { "Top Customers" = "1" }` should get `✓`.

## Gotchas

- Proven on 2026-09-26: Tableau reads range-filter dates as UTC days, so `2024-01-01` stays 1/1/2024 in Los Angeles and Auckland. Rerun the date recipe whenever `APPLY_JS` or the Embedding API version changes.
- Moving the start by one day may leave the CSV unchanged if that day has no rows (1/1/2024 has none on the test site). Use a range change that must drop rows as the control.
- The first view load can take tens of seconds. `VIZ_LOAD_TIMEOUT_MS` is 180 s, so wait for the process to exit instead of sleeping for a fixed time.
- Interactive `add`, flag `add`, `run`, and `verify.py inspect` reject a story view with `Stories are not supported; use the dashboard inside it.` The supported target is the dashboard inside the story. Relative dates and dates computed at run time are refused on range bounds; use a parameter or an absolute `YYYY-MM-DD` or `M/D/YYYY` date.
- `Dashboard1` has parameters `Top Customers` (5) and `Profit Bin Size` (200), but neither changes `B Real Sheet`: on 2026-09-26 both param jobs had the same sha256 as the unfiltered export. That's why the parameter recipe proves the path with an unknown name. For a value-effect proof, find a view whose sheet actually uses a parameter.
