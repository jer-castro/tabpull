# REST export

`run` signs in with the PAT and pulls each `method = "rest"` job's summary data through the REST API with no browser, writing the CSV Tableau returns to `exports/<job>/<view name>.csv` (both names slugged, so spaces become `_`). It's the view's summary data, not the crosstab layout, and for a dashboard it's only the first sheet.

## Sub-features

- `rest-export` exports a published view by `view_id` and names the CSV after the view.
- `rest-values` sends each `values` filter as a `vf_` filter and each `params` entry as a parameter.
- `rest-comma` escapes a comma inside a value as `\,`, so `Smith, Jane` stays one value.
- `rest-limits` rejects range filters on REST jobs, and rejects several filters when any has more than one value, when the jobs file loads.
- `rest-failure` reports `✗ <job>: <reason>` for a bad `view_id`, keeps going, and exits 1.

## How to get to it (user POV)

- `uv run src/crosstab.py add` on a published worksheet, then pick `[2] summary data via REST API`.
- A hand-written `[[job]]` with `method = "rest"` and `view_id` in `jobs.toml`, then `uv run src/crosstab.py run [name ...]`.

## Driving it with the CLI

Preconditions:

- `settings` and `jobs.toml` are `ok` in doctor. REST drives don't need the browser session: a run with only REST jobs never opens a browser.
- No other REST drive is running (a PAT sign-in ends other sessions on that PAT).

- **Unfiltered.** Write `$E/rest/jobs.toml` with two REST jobs: `rest-all` on `view = "Superstore/OrderDetails"`, `view_id = "011b479c-65d3-43ca-a855-8ae63ffac15e"`, and `world-pop` on `view = "WorldIndicators/Population"`, `view_id = "7355995d-1ae5-4fa4-a677-7713d2fb9f76"`. Run `uv run src/crosstab.py --jobs $E/rest/jobs.toml --out $E/rest/exports run 2>&1 | tee $E/rest/run.log`. You should get `✓ rest-all: .../rest-all/Order_Details.csv` and `✓ world-pop: .../world-pop/Population.csv`, and exit 0. On 2026-09-26 they had 35778 and 208 lines; `Population.csv`'s header includes `Country/Region`.
- **Comma inside a value.** `Country/Region` in `Population.csv` is the only known dimension with commas in its values (3 rows). Build the job without printing the value: `uv run python -c "import csv,json; rows=list(csv.DictReader(open('$E/rest/exports/world-pop/Population.csv', encoding='utf-8-sig'))); v=next(r['Country/Region'] for r in rows if ',' in r['Country/Region']); open('$E/rest/comma.toml','w').write('[[job]]\nname = \"world-comma\"\nmethod = \"rest\"\nview = \"WorldIndicators/Population\"\nview_id = \"7355995d-1ae5-4fa4-a677-7713d2fb9f76\"\nfilters = [{ field = \"Country/Region\", values = [' + json.dumps(v) + '] }]\n')"`, then `run` it with `--jobs $E/rest/comma.toml`. The CSV must have exactly 2 lines (header plus the one matching row), and every row's `Country/Region` must equal the requested value (compare in Python, print only the boolean). A header-only CSV means the comma split the value into two that match nothing.
- **Values control.** The `world-comma` sha256 must differ from `world-pop`'s.
- **Range filter rejected.** Add a REST job with `filters = [{ field = "Order Date", min = "2024-01-01" }]` to a separate `$E/rest-bad/jobs.toml` and run it. It should exit 1 before any export with `$E/rest-bad/jobs.toml: job '<name>': REST jobs only support `values` filters`.
- **Several multi-value filters rejected.** Same, with two filters where one has two values. It should exit 1 with `$E/rest-bad/jobs.toml: job '<name>': REST jobs with several filters take one value per filter; use an embed job to filter on several values`.
- **Failure path.** Add a job with `view_id = "00000000-0000-0000-0000-000000000000"` next to a good one and run both. You should get `✗ <job>: ServerResponseError("...404006: Resource Not Found...")` on one line, `✓` for the good one, and exit 1.

## Gotchas

- REST exports use `maxAge=1`, so Tableau may serve data up to a minute old. Don't read a diff between two back-to-back runs as a filter effect without a control.
- Tableau pairs `vf_` values across keys by position instead of cross-filtering them. That's why `RestJob` refuses several filters with more than one value; use an embed job for those.
- The CSV is written as Tableau sends it (UTF-8, often with a BOM). Only embed exports go through `normalize_csv`.
- `add` offers REST only for a published worksheet (`sheetType` `view`), so a REST job on a dashboard like `Superstore/OrderDetails` has to be written by hand. Its REST export is not the embedded `Product Name` sheet: the header is `Customer Name, Measure Names, Order Date, Order ID, Ship Date, Ship Mode, Measure Values`, and only `Measure Values` (formatted numbers) contains commas, so it can't prove `rest-comma`.
- Signing in with the PAT ends every other session on that PAT, the user's own scripts included. Run REST drives one at a time.
