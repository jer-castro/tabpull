# tabpull

Exports crosstab CSVs from Tableau dashboards from the command line. It does what you'd do by hand (open the dashboard, set filters, then Download → Crosstab → CSV) for any sheet in the dashboard. That includes hidden sheets and ones that aren't first alphabetically, and it works on sites where you're allowed to crosstab but not to download the workbook.

## Why not just the REST API

The REST crosstab/data endpoints only export the first sheet when the view is a dashboard, and hidden (dashboard-only) sheets have no REST view at all. So dashboard sheets go through the Tableau Embedding API (`exportCrosstabAsync`) in a headless browser signed in with your SSO session. The PAT is still used to find views.

## Install

From a checkout of this repo:

```sh
uv tool install .
```

That installs one `tabpull` command. `tabpull add`, `tabpull run`, and `tabpull login` are that command.

## Setup

```sh
uv run src/wizard.py
```

The wizard asks for a dashboard URL (gives server + site), walks you through creating a personal access token and checks it, then opens a browser for your normal SSO sign-in. It writes `TABLEAU_SERVER_URL`, `TABLEAU_SITE`, `TABLEAU_PAT_NAME` and `TABLEAU_PAT_SECRET` to `.env` and the browser session to `.auth/tableau-state.json`. That file holds live session cookies, so keep it private (it's gitignored). The browser used is your installed Chrome or Edge; if you have neither, run `uv run playwright install chromium`.

## Use

```sh
uv run src/crosstab.py add            # search a view by name or paste its URL, pick sheets + filters, save a job
uv run src/crosstab.py run            # export every job in jobs.toml to exports/<job>/<sheet>.csv
uv run src/crosstab.py run daily-west # only some jobs
uv run src/crosstab.py login          # refresh the SSO session
```

`add` saves the job and prints the `run` command to export it. `run` keeps going when one job fails and exits non-zero if any did. When the SSO session has expired it opens the sign-in window if you're at a terminal, and otherwise exits and asks you to run `login`. Crosstab CSVs are rewritten from Tableau's UTF-16 tab-separated format to plain UTF-8 CSV.

## Jobs file

`add` writes these for you, and you can also edit `jobs.toml` by hand:

```toml
[[job]]
name = "daily-west"
view = "SalesWorkbook/Overview"        # Workbook/View from the URL
sheets = ["Order Detail", "Totals"]    # any worksheet in the dashboard, hidden ones included
filters = [
  { field = "Region", values = ["West", "Central"] },
  { field = "Order Date", min = "2026-09-01", max = "2026-09-25", sheet = "Totals" },
]
params = { "Top N" = "25" }
```

Parameters are set first, then filters, in the same order you'd set them in the dashboard. A filter applies on `sheet` (default: the first entry in `sheets`) and reaches other sheets the same way it does in the UI. Dates written as `YYYY-MM-DD` or `M/D/YYYY`, and plain numbers, are converted for range filters.

Not covered: stories, relative-date filters, and dates computed at run time. Use a parameter or edit the job for those.

## Develop

```sh
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
```
