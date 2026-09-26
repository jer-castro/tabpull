# SSO session

Embed exports and `add` run in a headless browser that reuses the SSO cookies saved in `.auth/tableau-state.json`. Before each use the CLI asks Tableau's `getSessionInfo` whether the cookies are still live (a missing file counts as expired without asking). When they aren't, it opens the sign-in window at a terminal and otherwise exits and asks for `login`.

## Sub-features

- `session-reuse` loads `.auth/tableau-state.json` headless and uses it when `getSessionInfo` answers OK.
- `session-expired-nontty` exits with `Tableau browser session is missing or expired. Run: uv run src/crosstab.py login` when stdin isn't a terminal.
- `session-expired-tty` opens the visible sign-in window from `run` or `add`, then carries on headless.
- `login` opens a visible browser at the site home, polls every 2 s until the session is live, saves the cookies with mode 600, and prints `Saved browser session to .auth/tableau-state.json`.
- `login-timeout` gives up with `Gave up waiting for sign-in after 300s.`, and a closed window ends with `The browser closed before sign-in finished.`
- `settings-missing` exits with `Missing <keys> in .env. Run: uv run src/wizard.py` when stdin isn't a terminal, and prints `Missing <keys> in .env. Starting setup.` then starts the wizard when it is.

## How to get to it (user POV)

- `uv run src/crosstab.py login` refreshes the session on purpose.
- `uv run src/crosstab.py run` or `add` hits the session check before the first embed view opens.
- The setup wizard (`uv run src/wizard.py`) ends with the same sign-in window unless the user answers no to `Sign in now?`.

## Driving it with the CLI

Preconditions:

- `settings` is `ok` in doctor. The `session` line decides which paths can be driven: `ok` proves reuse, `FAIL` means only a human can continue.
- Every drive below works from a scratch directory, because `.env`, `.auth/` and `jobs.toml` resolve relative to the current directory. Never touch the real `.auth/tableau-state.json`.

- **Reuse.** With doctor `ok`, any embed drive from [run-embed.md](./run-embed.md) proves it: the log has `✓` lines and no sign-in window prompt.
- **Missing session, no tty.** `mkdir -p $E/login/nosession && cp .env $E/login/nosession/` and write an embed job for `CrosstabMe/Dashboard1` / `B Real Sheet` to `$E/login/nosession/jobs.toml`. From that directory run `uv run --project <repo root> <repo root>/src/crosstab.py run </dev/null 2>&1 | tee run.log`. It should exit 1 with the `Run: uv run src/crosstab.py login` message, open no window, and write no CSV.
- **Expired session, no tty.** Same directory, plus `mkdir .auth && echo '{"cookies":[],"origins":[]}' > .auth/tableau-state.json`. The run should end with the same message. That proves a state file without a live `XSRF-TOKEN` is treated as expired rather than used.
- **Missing settings, no tty.** Run from an empty scratch directory with no `.env` and none of the `TABLEAU_*` variables exported: `run </dev/null` should exit with `Missing TABLEAU_SERVER_URL, TABLEAU_SITE, TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET in .env. Run: uv run src/wizard.py`.
- **`login`, tty paths, timeout.** Human only. Report them as unverified and ask the user to run `uv run src/crosstab.py login`. Proof afterwards is doctor's `session ok` line with mode `600`.

## Gotchas

- Environment variables override `.env` per key, so an exported `TABLEAU_*` variable can make a "missing settings" drive pass by accident. Check `env | grep -c '^TABLEAU_'` is 0 first.
- An empty `TABLEAU_SITE` is valid (Tableau Server default site); only a missing key counts as missing.
- `login` reuses the existing state file if there is one, so an SSO provider that still holds a session may close the window almost at once. That's still a real refresh.
- The browser is installed Chrome, then Edge, then Playwright's Chromium. With none installed every browser path exits with `No Chrome or Edge found. Install one, or run: uv run playwright install chromium`.
- `session_valid` calls an internal Tableau endpoint. If every drive reports an expired session right after a successful `login`, suspect a changed `getSessionInfo` before suspecting the cookies.
