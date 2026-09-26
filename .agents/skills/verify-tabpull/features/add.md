# Add a job

`add` finds a view from a pasted URL or a loosely typed name, lets the user pick sheets, filters and parameters, appends the job to the jobs file, and prints `Run it: tabpull run <name>`. With `--view` and `--sheet` it writes that job from flags and does not prompt. It never exports. The job's `site` is the local name from `tabpull setup`.

## Sub-features

- `add-url` matches a pasted view URL exactly.
- `add-fuzzy` ranks views whose name plus content URL match every query word (score at least `MIN_MATCH_SCORE` 0.75, so typos pass), and says `Showing the best 30 of N matches` when it trims the list.
- `add-sheets` lists every sheet in a dashboard, hidden ones too, then after the pick prints each sheet's filters with their type and current value, plus the parameters.
- `add-filters` turns `Field=a|b` into a values filter, and `Field=min..max` into a range filter only when that field is a range filter on the view. A name that isn't a filter there gets a note that it names no sheet and goes on the first chosen sheet as a values filter.
- `add-flags` accepts `--site`, `--view`, `--sheet` (repeatable), `--filter`, `--param`, and `--name`, writes the job, and does not prompt or call Tableau. A `--filter` without ` @Sheet` is saved on the first `--sheet` after a note. Omit `--view` and `--sheet` and `add` still asks.
- `add-save` appends a `[[job]]` block, records `site`, and refuses a duplicate job name.

## How to get to it (user POV)

- `tabpull add`, or `tabpull add --site <site>`, then answer the prompts.
- `tabpull add --site <site> --view Workbook/View --sheet "Sheet"` writes a job with no prompts.

## Driving it with the CLI

Preconditions:

- Doctor is `ok`. `<site>` is the local name on the settings line.
- `$E/add/` exists and has no `jobs.toml` yet.

- **Fuzzy name.** Run `printf 'crostab dashbord\n1\nverify-add\n2\nShip Mode=First Class\n\n\n' | uv run tabpull --site <site> --jobs $E/add/jobs.toml --out $E/add/exports add 2>&1 | tee $E/add/add.log`. The log should list `[1] Dashboard 1  (dashboard, CrosstabMe/sheets/Dashboard1)`, list both sheets, print the three `filter` lines for `B Real Sheet`, and end with `Saved job 'verify-add' to $E/add/jobs.toml:` followed by `Run it: tabpull run verify-add`. `$E/add/jobs.toml` should contain `site = "<site>"` and `filters = [{ "field" = "Ship Mode", "values" = ["First Class"], "sheet" = "B Real Sheet" }]`.
- **No export.** That drive passes `--out $E/add/exports`. After it, `$E/add/exports` must not exist. `add` never exports.
- **URL.** Repeat with `https://10ax.online.tableau.com/#/site/jercastro/views/CrosstabMe/Dashboard1` as the first answer and a new job name. It should list exactly one view, and `Which view?` still needs the `1`.
- **No match.** Pipe `zzqx\n`. It should exit with `No views you can access match 'zzqx'.`
- **Duplicate name.** Rerun the fuzzy drive against the same jobs file with `verify-add` as the name. It should exit with `A job named 'verify-add' already exists in $E/add/jobs.toml.`, and the file should still hold one job.
- **Flags.** `uv run tabpull --jobs $E/add/flags.toml add --site <site> --view CrosstabMe/Dashboard1 --sheet "B Real Sheet" --sheet "A Title Sheet" --filter "Ship Mode=First Class" --name verify-flags`. It should not prompt. The log should say filter `Ship Mode` names no sheet and is applied on `B Real Sheet`. The file should record that sheet, `site = "<site>"`, and no second filter entry. `$E/add/exports` must not exist.

## Gotchas

- The answer after the job name picks sheets by number in the printed order, which is not alphabetical. A published worksheet goes straight to that sheet picker, the same as a dashboard.
- `add` asks for the URL or name first, then downloads the full view list, unless `--view` is set. On a very large site the wait comes before the match list. Flag add does not download the view list.
- Pass `--site <site>` in piped drives. With more than one configured site and no `--site`, a site picker is the first prompt.
