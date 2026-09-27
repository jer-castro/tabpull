<p align="center">
  <img src="https://raw.githubusercontent.com/jer-castro/tabpull/main/assets/hero.svg" alt="tabpull - crosstab any sheet, skip the workbook" width="100%" />
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-d1dedc?style=flat-square" alt="MIT license" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.12%2B-8ab4ff?style=flat-square" alt="Python 3.12+" /></a>
</p>

<p align="center">
  Dashboard-only sheets are included. tabpull uses the Embedding API with your SSO session, and a personal access token finds the views.
</p>

<p align="center">
  <a href="#what-it-automates">What it automates</a> ·
  <a href="#why-not-the-rest-api">Why not the REST API</a> ·
  <a href="#install">Install</a> ·
  <a href="#setup">Setup</a> ·
  <a href="#use">Use</a> ·
  <a href="#where-files-live">Where files live</a> ·
  <a href="#jobs-file">Jobs</a> ·
  <a href="#develop">Develop</a>
</p>

---

## What it automates

tabpull is one command, `tabpull`, for the export you'd otherwise click through in Tableau: open a published dashboard, set the filters and parameters you'd set by hand, then Download → Crosstab → CSV.

- **Any named worksheet in the dashboard.** That includes sheets that only exist inside the dashboard and aren't published as their own views, and sheets that aren't first alphabetically.
- **Saved jobs.** `tabpull add` records the view, sheets, filters, and parameters as a job. `tabpull run` exports it later, one UTF-8 CSV per sheet.
- **Several sites.** `tabpull setup` configures each Tableau server or site once, and each job names the site it uses.
- **No workbook download.** It works on sites where you're allowed to crosstab but not to download the workbook.

## Why not the REST API

The REST crosstab/data endpoints only export the first sheet when the view is a dashboard. A worksheet that lives only inside a published dashboard (often called a hidden sheet) is not published as its own view, so it has no REST view at all.

Dashboard sheets go through the Tableau Embedding API (`exportCrosstabAsync`) in a headless browser signed in with your SSO session. The PAT is still used to find views.

## Install

```sh
uv tool install git+https://github.com/jer-castro/tabpull
```

That installs one `tabpull` command. `setup`, `add`, `run`, and `login` are subcommands. Pin a tag with `@vX.Y.Z` on that URL, and upgrade with `uv tool upgrade tabpull`.

## Setup

```sh
tabpull setup
tabpull setup --site finance   # local name jobs will use
```

The wizard asks for that name (unless you passed `--site`), a dashboard URL, and a personal access token. It checks the token, then opens a browser for your normal SSO sign-in.

Run it again for another Tableau server or site. Each site keeps its own token and browser session. Re-running a name keeps the current values when you press Enter.

The browser is your installed Chrome, then Edge. If neither is installed, tabpull prints `uvx playwright==<version> install chromium` for the Playwright version it has. tabpull does not download a browser while Chrome or Edge is already there.

## Use

```sh
tabpull                             # sites, saved jobs, and where exports go
tabpull add                         # prompts: search a view or paste its URL, pick sheets and filters
tabpull add --site finance \
  --view SalesWorkbook/Overview \
  --sheet "Order Detail" --sheet Totals \
  --filter "Region=West|Central" \
  --filter "Order Date=2026-09-01..2026-09-25 @Totals" \
  --param "Top N=25" \
  --name daily-west                 # same result, no prompts
tabpull run                         # export every job into the current folder
tabpull run daily-west              # only some jobs
tabpull login                       # refresh SSO for the only site
tabpull login --site finance        # refresh one site when several are configured
tabpull --version
```

`tabpull` with no command prints the configured sites, the saved jobs, the jobs file, the output folder, and the next commands to try. A terminal shows those as panels; a pipe gets the same listing in [TOON](https://toonformat.dev/). Errors, including an unknown flag, print `error: ...` on stdout with the fix or the command's usage. A usage error exits 2 and any other failure exits 1.

`add` saves the job and prints the `run` command. It does not export. With `--view` and at least one `--sheet`, `add` writes the job and does not prompt. Leave those flags off and it asks.

`add` with `--view` and `--sheet` still opens the view far enough to refuse a story, with the same message as interactive add: `Stories are not supported; use the dashboard inside it.` `run` refuses a story the same way, before it exports.

`--filter` is `Field=a|b` or `Field=min..max`, and ` @Sheet` names the worksheet. Either side of a range may be omitted, but not both: `Field=min..` runs from that date through the latest value the filter allows, and `Field=..max` runs from the earliest value through that date. An open side needs a range filter on that field in the workbook, since tabpull reads its endpoint from there. A filter with no sheet is applied on the first sheet in the job, and tabpull prints that. With one configured site, `--site` can be omitted. With several, pass `--site` or pick one at the prompt.

`run` keeps going when one job fails. A terminal shows a progress bar and a summary panel titled `done: <ok>/<total> jobs exported`; a pipe prints that same `done` line and a check or cross per job. If any job failed it prints the `tabpull run` command that reruns only those and exits 1. Each job uses the site it names. When that site's SSO session is missing or expired, tabpull opens the sign-in window if you're at a terminal, and otherwise exits and tells you to run `tabpull login --site <name>`. Crosstab CSVs are rewritten from Tableau's UTF-16 tab-separated format to plain UTF-8 CSV.

`run` takes the same repeatable `--filter` as `add`. Each override applies to every job in that run and is not written back to the jobs file. With no ` @Sheet` it replaces the job's saved filter on that field on every sheet; with ` @Sheet` it replaces only that sheet's. A field the job does not already filter is added on ` @Sheet`, or with no sheet on the first sheet, and tabpull prints that. Leave one side of `min..max` empty when that end should stay the filter's own limit. Compute the dates in the caller (tabpull still refuses a relative date) and point each run at its own folder:

```sh
start=2026-09-01
end=$(date +%F)
tabpull run daily-west \
  --filter "Order Date=${start}.." \
  --out "./exports/${end}"
tabpull run daily-west weekly-east \
  --filter "Order Date=${start}..${end}" \
  --out "./exports/${end}"
```

`run` writes `<job>/<sheet>.csv` under the folder you run it from (spaces in names become `_`). Point one run at another jobs file or output folder, before or after the subcommand:

```sh
tabpull run daily-west --jobs ./jobs.toml --out ./exports
```

## Where files live

Tokens, cookies, and jobs stay in the config directory, and they stay put when you change the working directory. Exports go to the working directory, or `--out`. Deleting the config directory removes tabpull's settings from the machine. A `.env` file and `TABLEAU_*` environment variables are not read.

| | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Config (site tokens, SSO cookies, `jobs.toml`) | `$XDG_CONFIG_HOME/tabpull` or `~/.config/tabpull` | `~/Library/Application Support/tabpull` | `%APPDATA%\tabpull` |

`XDG_CONFIG_HOME` wins on every OS when it is set.

```text
<config>/tabpull/sites/<name>.env     server, Tableau site, token name, token secret
<config>/tabpull/jobs.toml
<config>/tabpull/auth/<name>.json     SSO cookies; mode 600
./<job>/<sheet>.csv                   exports, relative to where you ran tabpull
```

The token secret is not printed. Setup and login print the server and the Tableau site.

## Jobs file

`add` writes these for you, and you can also edit the jobs file by hand:

```toml
[[job]]
name = "daily-west"
site = "finance"                       # local name from `tabpull setup`
view = "SalesWorkbook/Overview"        # Workbook/View from the URL
sheets = ["Order Detail", "Totals"]    # any worksheet in the dashboard, dashboard-only ones included
filters = [
  { field = "Region", values = ["West", "Central"] },
  { field = "Order Date", min = "2026-09-01", max = "2026-09-25", sheet = "Totals" },
]
params = { "Top N" = "25" }
```

Parameters are set first, then filters, in the same order you'd set them in the dashboard. A filter applies on `sheet`. When `sheet` is omitted, tabpull applies that filter on the first entry in `sheets` and prints a line saying so. It does not copy the filter onto every sheet. Other sheets update only as they would in the dashboard when that filter changes.

Dates written as `YYYY-MM-DD` or `M/D/YYYY`, and plain numbers, are converted for range filters. An impossible date such as `2024-02-31` or `2/31/2024` is rejected. A relative date (`yesterday`, `last week`, `today`, `7 days ago`) or a date computed at run time is refused. Use a parameter, or write an absolute `YYYY-MM-DD` or `M/D/YYYY` date. Omit `min` or `max` to leave that end open: `{ field = "Order Date", min = "2026-09-01", sheet = "Totals" }` runs from that date through the latest value the filter allows, and `{ field = "Order Date", max = "2026-09-25", sheet = "Totals" }` runs from the earliest value through that date.

Stories are refused; use the dashboard inside the story.

## Develop

From a checkout of this repo, `uv tool install .` installs the command, and `uv run tabpull` runs it without installing. `uv run playwright install chromium` installs the Chromium fallback for that checkout. An installed `tabpull` prints `uvx playwright==<version> install chromium` for the Playwright version it has.

```sh
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
```
