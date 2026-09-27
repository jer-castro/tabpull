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

## Features

- [Embed export](./run-embed.md) covers dashboard sheet crosstabs, hidden sheets, values filters, date ranges and parameters.
- [Add a job](./add.md) covers view search by URL or fuzzy name, picking sheets and filters, flag add, and saving the job.
- [SSO session](./login.md) covers per-site session reuse, the expired-session paths, and `login`.
