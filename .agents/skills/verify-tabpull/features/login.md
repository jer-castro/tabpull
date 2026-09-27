# SSO session

Embed exports and `add` (interactive, and flag `add` when it opens the view to refuse a story) run in a headless browser that reuses the SSO cookies saved at `<config>/tabpull/auth/<name>.json` for the site the job names. Before each use the CLI asks Tableau's `getSessionInfo` whether the cookies are still live (a missing file counts as expired without asking). When they aren't, it opens the sign-in window at a terminal and otherwise exits and asks for `tabpull login --site <name>`.

## Sub-features

- `session-reuse` loads that site's auth file headless and uses it when `getSessionInfo` answers OK.
- `session-expired-nontty` fails with `Tableau browser session for site '<name>' is missing or expired. Run: tabpull login --site <name>` when stdin isn't a terminal. `run` prints it as `✗ <job>: ...` and exits 1.
- `session-expired-tty` opens the visible sign-in window from `run` or `add`, then carries on headless.
- `login` opens a visible browser at the site home, polls every 2 s until the session is live, saves the cookies with mode 600, and prints `Saved browser session to <config>/tabpull/auth/<name>.json`. It also prints the server and the Tableau site, and it does not print the token.
- `login-timeout` gives up with `Gave up waiting for sign-in after 300s.`, and a closed window ends with `The browser closed before sign-in finished.`
- `settings-missing` with no configured site exits with `No Tableau site configured. Run: tabpull setup` when stdin isn't a terminal, and starts setup when it is. A site file that is missing keys names those keys, the file path, and `tabpull setup --site <name>`.

## How to get to it (user POV)

- `tabpull login` refreshes the only configured site. `tabpull login --site <name>` refreshes one site when several exist.
- `tabpull run` or interactive `add` hits the session check before the first embed view opens.
- `tabpull setup` ends with the same sign-in window unless the user answers no to `Sign in now?`.

## Driving it with the CLI

Preconditions:

- `settings` is `ok` in doctor. The `session` line decides which paths can be driven: `ok` proves reuse, `FAIL` means only a human can continue.
- Negative tests set `XDG_CONFIG_HOME` and `XDG_DATA_HOME` inside `$E` and copy the site's env file from the real `<config>/tabpull/sites/<name>.env` (the directory above the auth path doctor prints) into `$XDG_CONFIG_HOME/tabpull/sites/<name>.env`. They must not copy or edit the real auth file. Run them with `.venv/bin/tabpull`, not `uv run`, so uv doesn't rebuild the repo `.venv` under the scratch `XDG_DATA_HOME`. Unset those variables for every other drive.

- **Reuse.** With doctor `ok`, any embed drive from [run-embed.md](./run-embed.md) proves it: the log has `✓` lines and no sign-in window prompt.
- **Missing session, no tty.** Copy only `<name>.env` into the scratch config `sites/` directory (no auth file). Write an embed job for `CrosstabMe/Dashboard1` / `B Real Sheet` with `site = "<name>"` to `$E/login/jobs.toml`. Run `XDG_CONFIG_HOME=$E/login/config XDG_DATA_HOME=$E/login/data .venv/bin/tabpull --jobs $E/login/jobs.toml --out $E/login/exports run </dev/null 2>&1 | tee $E/login/nosession.log`. It should exit 1 with `✗ <job>: ... Run: tabpull login --site <name>`, open no window, and write no CSV.
- **Expired session, no tty.** Same scratch config, plus `mkdir -p $XDG_CONFIG_HOME/tabpull/auth && echo '{"cookies":[],"origins":[]}' > $XDG_CONFIG_HOME/tabpull/auth/<name>.json`. The run should end with the same message, and doctor under the same variables should print `session FAIL`. That proves a state file without a live `XSRF-TOKEN` is treated as expired rather than used.
- **Missing settings, no tty.** With `XDG_CONFIG_HOME` pointed at an empty directory, `.venv/bin/tabpull login </dev/null` should exit 1 with `No Tableau site configured. Run: tabpull setup`.
- **`login`, tty paths, timeout.** Human only. Report them as unverified and ask the user to run `tabpull login --site <name>`. Proof afterwards is doctor's `session ok` line with mode `600`.

## Gotchas

- `TABLEAU_*` environment variables and a `.env` file are not a site. A scratch directory's `.env` does not configure tabpull. Only the site file under the config directory does.
- An empty `TABLEAU_SITE` is valid (Tableau Server default site); only a missing key counts as missing.
- `login` reuses the existing state file if there is one, so an SSO provider that still holds a session may close the window almost at once. That's still a real refresh.
- The browser is installed Chrome, then Edge, then Playwright's Chromium. With none installed every browser path exits with `No Chrome or Edge found. Install one, or run: uvx playwright==<version> install chromium`, where `<version>` is the Playwright package tabpull has installed.
- `session_valid` calls an internal Tableau endpoint. If every drive reports an expired session right after a successful `login`, suspect a changed `getSessionInfo` before suspecting the cookies.
- Two sites do not share cookies. `login` with more than one site and no `--site` asks which site at a terminal, and exits asking for `--site` when stdin is not a terminal.
