# Add a job

`add` finds a view from a pasted URL or a loosely typed name, lets the user pick sheets, filters and parameters, appends the job to the jobs file, and offers to run it.

## Sub-features

- `add-url` matches a pasted view URL exactly.
- `add-fuzzy` ranks views whose name plus content URL match every query word (score at least `MIN_MATCH_SCORE` 0.75, so typos pass), and says `Showing the best 30 of N matches` when it trims the list.
- `add-sheets` lists every sheet in a dashboard, hidden ones too, then after the pick prints each sheet's filters with their type and current value, plus the parameters.
- `add-filters` turns `Field=a|b` into a values filter, and `Field=min..max` into a range filter only when that field is a range filter on the view. A name that isn't a filter there gets a warning and goes on the first chosen sheet as a values filter.
- `add-save` appends a `[[job]]` block and refuses a duplicate job name.

## How to get to it (user POV)

- `uv run src/crosstab.py add`, then answer the prompts.

## Driving it with the CLI

Preconditions:

- Doctor is `ok`.
- `$E/add/` exists and has no `jobs.toml` yet.

- **Fuzzy name.** Run `printf 'crostab dashbord\n1\nverify-add\n2\nShip Mode=First Class\n\n\nn\n' | uv run src/crosstab.py --jobs $E/add/jobs.toml add 2>&1 | tee $E/add/add.log`. The log should list `[1] Dashboard 1  (dashboard, CrosstabMe/sheets/Dashboard1)`, list both sheets, print the three `filter` lines for `B Real Sheet`, and print `Saved job 'verify-add' to $E/add/jobs.toml:` before `Run it now? [Y/n]`. `$E/add/jobs.toml` should contain `filters = [{ "field" = "Ship Mode", "values" = ["First Class"], "sheet" = "B Real Sheet" }]`.
- **URL.** Repeat with `https://10ax.online.tableau.com/#/site/jercastro/views/CrosstabMe/Dashboard1` as the first answer and a new job name. It should list exactly one view, and `Which view?` still needs the `1`.
- **No match.** Pipe `zzqx\n`. It should exit with `No views you can access match 'zzqx'.`
- **REST choice.** Run `printf 'superstore performance\n1\nverify-rest\n2\n\n\ny\n' | uv run src/crosstab.py --jobs $E/add-rest/jobs.toml --out $E/add-rest/exports add 2>&1 | tee $E/add-rest/add.log`. The log should show `This is a published worksheet`, save a job with `method = "rest"`, print `✓ verify-rest`, and the command should exit 0 (in zsh read `${pipestatus[2]}`, in bash `${PIPESTATUS[1]}`). The CSV had 429 lines on 2026-09-26.
- **Duplicate name.** Rerun the fuzzy drive against the same jobs file with `verify-add` as the name. It should exit with `A job named 'verify-add' already exists in $E/add/jobs.toml.`, and the file should still hold one job.

## Gotchas

- The prompt order changes if the view is a published worksheet, which adds a REST/browser choice after the job name. The listing only carries `sheetType` because `_find_view` asks for it (`fields=_default_,sheetType`), and a published worksheet reports `view`, not `worksheet`. If that choice stops appearing for `Superstore/Performance`, check those two things first. A dashboard reports `dashboard`, so if `Dashboard 1` ever lists as `view` the fuzzy recipe's answers shift by one prompt.
- For a dashboard, the answer after the job name picks sheets by number in the printed order, which is not alphabetical. A REST job has no sheet picker.
- `add` asks for the URL or name first, then downloads the full view list. On a very large site the wait comes before the match list.
