# tabpull verification map

This directory is the maintained source for verifying what a user of `tabpull` sees. Read this index, then use the matching feature file as the recipe.

## Baseline preconditions

- Repo root is the working directory, and `uv sync` has run.
- `verify.py doctor` reports `ok` on every settings, session, and jobs line.
- `RUN=$(date +%Y%m%d-%H%M%S); E=.verify/$RUN`. Every drive uses `--jobs $E/<feature>/jobs.toml --out $E/<feature>/exports`.
- Never write to the config-directory jobs file. Exports default to the working directory, so always pass `--out` or the repo root fills with job folders.

## Driving conventions

- CLI drives are literal commands. Prompts are answered by piped stdin, in prompt order.
- `<site>` is the local name doctor prints on `settings ok`. Pass `--site <site>` when more than one site is configured.
- Pick sheet and field names from `verify.py --site <site> inspect Workbook/View` output, not from memory.
- Run interactive `add` drives one at a time, because a PAT sign-in ends other sessions.

## Proof and skip reporting

- Capture the command, the combined stdout/stderr, and the exit code in `$E/<feature>/*.log`.
- Prove exports by file path plus line count or sha256, never row contents.
- Every filter proof includes a control export that must differ.
- Record the feature ID with every artifact. Report a human-only path (SSO window, wizard) as unverified, and name the command a human must run.

## Feature entry contract

Each feature file has an H1 and one paragraph, then exactly these H2s in order: `Sub-features`, `How to get to it (user POV)`, `Driving it with the CLI`, `Gotchas`.

## Open date ranges and run overrides

Drive these against `CrosstabMe/Dashboard1`, sheet `B Real Sheet`, field `Order Date`. Each filter proof needs a control export whose sha256 differs. Full steps are also in [Embed export](./run-embed.md).

- **Open max.** `uv run python .agents/skills/verify-tabpull/scripts/verify.py --site <site> date-filter CrosstabMe/Dashboard1 --sheet "B Real Sheet" --field "Order Date" --min 2024-01-01 --out $E/date-open-max`. Omit `--max`. It should `PASS`, with applied min `2024-01-01` and applied max equal to the filter's own maximum (the same day in every timezone, not null).
- **Open min.** The same command with `--max 2024-01-31` and no `--min`, `--out $E/date-open-min`. It should `PASS`, with applied max `2024-01-31` and applied min equal to the filter's own minimum.
- **Control.** `--min 2024-01-01 --max 2024-01-15 --out $E/date-open-control` should `PASS` with a sha256 that differs from both open runs.
- **run --filter.** Write `$E/override/jobs.toml` with one job, `site = "<site>"`, `view = "CrosstabMe/Dashboard1"`, `sheets = ["B Real Sheet"]`, `filters = [{ field = "Order Date", min = "2024-06-01", max = "2024-06-30", sheet = "B Real Sheet" }]`. Run `uv run tabpull --jobs $E/override/jobs.toml --out $E/override/exports run --filter "Order Date=2024-01-01.."`. Exit 0, and the jobs file is byte-for-byte unchanged. Repeat with `--filter "Order Date=2024-01-01..2024-01-15" --out $E/override-control/exports`. The two `B_Real_Sheet.csv` sha256 values must differ.

## Features

- [Embed export](./run-embed.md) covers dashboard sheet crosstabs, hidden sheets, values filters, date ranges, open bounds, `run --filter` overrides, and parameters.
- [Add a job](./add.md) covers view search by URL or fuzzy name, picking sheets and filters, flag add, and saving the job.
- [SSO session](./login.md) covers per-site session reuse, the expired-session paths, and `login`.
