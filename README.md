<p align="center">
  <img src="assets/hero.svg" alt="tabpull" width="100%" />
</p>

tabpull exports named worksheets from a published Tableau dashboard to UTF-8 CSV.

```sh
uv tool install git+https://github.com/jer-castro/tabpull
```

That installs the `tabpull` command. Bare `tabpull` at a terminal opens an interactive screen. `setup`, `add`, `remove`, `run`, and `login` are subcommands and work the same with or without it. Pin a tag with `@vX.Y.Z` on the URL. Upgrade with `uv tool upgrade tabpull`.

## Why Embedding API

REST crosstab and data endpoints only export the first sheet when the view is a dashboard. A worksheet that lives only inside a published dashboard is not published as its own view, so it has no REST view.

Those sheets go through the Embedding API (`exportCrosstabAsync`) in a headless browser signed in with your SSO session. The personal access token is used to find views.

## Setup

```sh
tabpull setup
tabpull setup --site finance   # local name jobs will use
```

The wizard asks for that name (unless you passed `--site`), a dashboard URL, and a personal access token. It checks the token, then opens a browser for SSO.

Run it again for another server or site. Each site keeps its own token and browser session. Re-running a name keeps the current values when you press Enter.

The browser is installed Chrome, then Edge. If neither is installed, tabpull prints `uvx playwright==<version> install chromium` for the Playwright version it has. It does not download a browser while Chrome or Edge is already there.

## Use

```sh
tabpull                             # interactive screen at a terminal; a listing when piped
tabpull add                         # search a view or paste its URL, pick sheets and filters
tabpull add --site finance \
  --view SalesWorkbook/Overview \
  --sheet "Order Detail" --sheet Totals \
  --filter "Region=West|Central" \
  --filter "Order Date=2026-09-01..2026-09-25 @Totals" \
  --param "Top N=25" \
  --name daily-west
tabpull run                         # every job, into the current folder
tabpull run daily-west
tabpull remove daily-west           # delete that saved job
tabpull remove                      # pick jobs to delete
tabpull login                       # refresh SSO for the only site
tabpull login --site finance
tabpull --version
```

With no command at a terminal, tabpull opens a full-screen list of sites and jobs. Open a job to change its filters (values or range bounds), parameters, sheets, view, site, and name. Each change is checked and saved to the jobs file right away. `r` runs the selected or marked jobs, `a` adds, `d` removes after a confirm, `s` and `l` run setup and login. Run, add, setup, and login leave the screen and run the normal subcommand in the terminal, then come back when you press Enter. The footer lists the keys, `?` shows all of them, `q` quits.

With no command and no terminal (a pipe or a script), tabpull prints configured sites, saved jobs, the jobs file, the output folder, and the next commands in [TOON](https://toonformat.dev/). It never waits for keys. Errors, including an unknown flag, print `error: ...` on stdout. A usage error exits 2. Any other failure exits 1.

`add` saves the job and prints the `run` command. It does not export. With `--view` and at least one `--sheet`, it writes the job and does not prompt. Leave those flags off and it asks.

`remove` deletes the named jobs and rewrites the jobs file. With no names, a terminal asks which jobs to delete and confirms before writing. With no terminal, pass the names. An unknown name exits 1 and lists the saved jobs, and the file is left unchanged. Removing every job leaves the file empty.

`add` with `--view` and `--sheet` still opens the view far enough to refuse a story: `Stories are not supported; use the dashboard inside it.` `run` refuses a story the same way, before it exports.

`--filter` is `Field=a|b` or `Field=min..max`. ` @Sheet` names the worksheet. Either side of a range may be omitted, but not both: `Field=min..` runs from that date through the latest value the filter allows, and `Field=..max` runs from the earliest value through that date. An open side needs a range filter on that field in the workbook, since tabpull reads its endpoint from there. A filter with no sheet is applied on the first sheet in the job, and tabpull prints that. With one configured site, `--site` can be omitted. With several, pass `--site` or pick one at the prompt.

`run` keeps going when one job fails. A terminal shows a progress bar and a summary panel titled `done: <ok>/<total> jobs exported`. A pipe prints that same `done` line and a check or cross per job. If any job failed, it prints the `tabpull run` command that reruns only those and exits 1. Each job uses the site it names. When that site's SSO session is missing or expired, tabpull opens the sign-in window at a terminal, and otherwise exits and tells you to run `tabpull login --site <name>`. Crosstab CSVs are rewritten from Tableau's UTF-16 tab-separated format to UTF-8 CSV.

`run --filter` uses the same repeatable syntax as `add`. Each override applies to every job in that run and is not written back to the jobs file. With no ` @Sheet` it replaces the job's saved filter on that field on every sheet. With ` @Sheet` it replaces only that sheet's. A field the job does not already filter is added on ` @Sheet`, or with no sheet on the first sheet, and tabpull prints that. Leave one side of `min..max` empty when that end should stay the filter's own limit. Relative dates are refused, so compute them in the caller:

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

`run --param` uses the same repeatable Name=value syntax as `add`. Each override applies to every job in that run and is not written back to the jobs file. A saved param of that name is replaced. A name the job does not have is added for this run. `tabpull run daily-west --param "Top N=10"`.

`run` writes `<job>/<sheet>.csv` under the current folder (spaces in names become `_`). `--jobs` and `--out` work before or after the subcommand:

```sh
tabpull run daily-west --jobs ./jobs.toml --out ./exports
```

## Where files live

Tokens, cookies, and jobs stay in the config directory. Exports go to the working directory, or `--out`. Deleting the config directory removes tabpull's settings. A `.env` file and `TABLEAU_*` environment variables are not read.

| | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Config (site tokens, SSO cookies, `jobs.toml`) | `$XDG_CONFIG_HOME/tabpull` or `~/.config/tabpull` | `~/Library/Application Support/tabpull` | `%APPDATA%\tabpull` |

`XDG_CONFIG_HOME` wins on every OS when it is set. `<config>` below is that directory.

```text
<config>/sites/<name>.env     server, Tableau site, token name, token secret
<config>/jobs.toml
<config>/auth/<name>.json     SSO cookies; mode 600
./<job>/<sheet>.csv           exports, relative to where you ran tabpull
```

The token secret is not printed. Setup and login print the server and the Tableau site.

## Jobs file

`add` writes this. `remove` and edits on the interactive screen rewrite it from the saved jobs, which drops comments. You can edit the file by hand:

```toml
[[job]]
name = "daily-west"
site = "finance"                       # local name from `tabpull setup`
view = "SalesWorkbook/Overview"        # Workbook/View from the URL
sheets = ["Order Detail", "Totals"]
filters = [
  { field = "Region", values = ["West", "Central"] },
  { field = "Order Date", min = "2026-09-01", max = "2026-09-25", sheet = "Totals" },
]
params = { "Top N" = "25" }
```

Parameters are set first, then filters. A filter applies on `sheet`. When `sheet` is omitted, tabpull applies that filter on the first entry in `sheets` and prints a line saying so. It does not copy the filter onto every sheet. Other sheets update only as they would in the dashboard when that filter changes.

`YYYY-MM-DD`, `M/D/YYYY`, and plain numbers are converted for range filters. An impossible date such as `2024-02-31` or `2/31/2024` is rejected. A relative date (`yesterday`, `last week`, `today`, `7 days ago`) or a date computed at run time is refused. Use a parameter, or write an absolute `YYYY-MM-DD` or `M/D/YYYY` date. Omit `min` or `max` to leave that end open: `{ field = "Order Date", min = "2026-09-01", sheet = "Totals" }` runs from that date through the latest value the filter allows, and `{ field = "Order Date", max = "2026-09-25", sheet = "Totals" }` runs from the earliest value through that date.

Stories are refused. Use the dashboard inside the story.

## Develop

From a checkout, `uv tool install .` installs the command, and `uv run tabpull` runs it without installing. `uv run playwright install chromium` installs the Chromium fallback for that checkout. An installed `tabpull` prints `uvx playwright==<version> install chromium` for the Playwright version it has.

```sh
uv run ruff check && uv run ruff format && uv run ty check && uv run pytest
```
