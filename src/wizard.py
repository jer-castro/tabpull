"""Setup wizard: walks you through the Tableau values the crosstab exporter needs.

Run: uv run src/wizard.py
Works on macOS, Linux, Windows and WSL. Safe to re-run: Enter keeps current values.
"""

import ctypes
import getpass
import itertools
import os
import platform
import shutil
import sys
import webbrowser

from playwright.sync_api import sync_playwright
from tableauserverclient.server.endpoint.exceptions import TableauError

from tableau import (
    AUTH_STATE,
    ENV_FILE,
    Settings,
    home_url,
    parse_tableau_url,
    read_env_file,
    rest_session,
    sso_login,
)

if os.name == 'nt':
    std_output_handle = -11
    enable_virtual_terminal_processing = 0x0004
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.GetStdHandle(std_output_handle)
    mode = ctypes.c_uint()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | enable_virtual_terminal_processing)

_COLOR = sys.stdout.isatty() and 'NO_COLOR' not in os.environ
BOLD, DIM, BLUE, GREEN, YELLOW, RESET = (
    ('\033[1m', '\033[2m', '\033[34m', '\033[32m', '\033[33m', '\033[0m')
    if _COLOR
    else ('',) * 6
)

_stage_numbers = itertools.count(1)
_written: list[str] = []
_skipped: list[str] = []


def _clear() -> None:
    if sys.stdout.isatty():
        print('\033[2J\033[3J\033[H', end='')


def banner(title: str) -> None:
    _clear()
    print(
        f'\n  {BOLD}{title}{RESET}\n  {DIM}{TOTAL_STAGES} stages. Values are saved to {ENV_FILE}.{RESET}\n'
    )
    pause('Press Enter to start…')


def stage(name: str) -> None:
    number = next(_stage_numbers)
    _clear()
    print(f'\n  {DIM}Stage {number} of {TOTAL_STAGES}{RESET}  {BOLD}{name}{RESET}\n')


def say(text: str) -> None:
    print(f'  {text}')


def step(text: str) -> None:
    print(f'  {BLUE}•{RESET} {text}')


def note(text: str) -> None:
    print(f'  {DIM}{text}{RESET}')


def warn(text: str) -> None:
    print(f'  {YELLOW}⚠ {text}{RESET}')


def open_url(url: str) -> None:
    """Open the human's normal browser, including from WSL."""
    say(f'Opening {url}')
    if 'microsoft' in platform.release().lower() and (
        opener := shutil.which('wslview') or shutil.which('explorer.exe')
    ):
        webbrowser.GenericBrowser(opener).open(url)
    elif not webbrowser.open(url):
        warn('Could not open a browser; open the URL above yourself.')


def pause(text: str = 'Press Enter when done…') -> None:
    input(f'\n  {DIM}{text}{RESET}')


def confirm(question: str, *, default: bool = False) -> bool:
    hint = 'Y/n' if default else 'y/N'
    answer = input(f'\n  {question} [{hint}] ').strip().lower()
    return default if not answer else answer in {'y', 'yes'}


def ask(key: str, prompt: str) -> str:
    """Visible input; on re-runs Enter keeps the value already in .env."""
    existing = read_env_file().get(key, '')
    suffix = f' {DIM}[{existing}]{RESET}' if existing else ''
    return input(f'  {prompt}{suffix} ').strip() or existing


def ask_secret(key: str, prompt: str) -> str:
    """Hidden input; on re-runs Enter keeps the value already in .env."""
    existing = read_env_file().get(key, '')
    suffix = ' (Enter keeps the current one)' if existing else ''
    return getpass.getpass(f'  {prompt}{suffix} ').strip() or existing


def write_env(key: str, value: str) -> None:
    """Upsert KEY=VALUE into .env, keeping every other line."""
    lines = (
        ENV_FILE.read_text(encoding='utf-8').splitlines() if ENV_FILE.exists() else []
    )
    kept = [line for line in lines if line.partition('=')[0].strip() != key]
    ENV_FILE.write_text('\n'.join([*kept, f'{key}={value}']) + '\n', encoding='utf-8')
    ENV_FILE.chmod(
        0o600
    )  # holds the PAT secret; on Windows this only keeps it writable
    _written.append(key)


def finish(next_step: str) -> None:
    _clear()
    print(f'\n  {GREEN}{BOLD}Done.{RESET}\n')
    for key in dict.fromkeys(_written):
        print(f'  {GREEN}✓{RESET} {key} → {ENV_FILE}')
    for item in _skipped:
        warn(item)
    print(f'\n  Next: {BOLD}{next_step}{RESET}\n')


TOTAL_STAGES = 3


def _site_stage() -> tuple[str, str]:
    stage('Tableau site')
    env = read_env_file()
    if server := env.get('TABLEAU_SERVER_URL'):
        note(
            f'Current: {server}, site {env.get("TABLEAU_SITE") or "(default)"}. Press Enter to keep it.'
        )
    step('In your usual browser, open any dashboard you normally crosstab from.')
    step('Copy the full URL from the address bar.')
    while True:
        url = input('\n  Paste the URL: ').strip()
        if not url and server:
            return server, env.get('TABLEAU_SITE', '')
        try:
            parsed = parse_tableau_url(url)
        except ValueError as e:
            warn(str(e))
            continue
        say(f'Server: {parsed.server}')
        say(f'Site:   {parsed.site or "(default site)"}')
        if confirm('Is that right?', default=True):
            write_env('TABLEAU_SERVER_URL', parsed.server)
            write_env('TABLEAU_SITE', parsed.site)
            return parsed.server, parsed.site


def _pat_stage(server: str, site: str) -> Settings:
    stage('Personal access token')
    say(
        'The token lets the exporter find views and pull published sheets without a browser.'
    )
    open_url(home_url(server, site))
    step('Click your profile picture (top right) → My Account Settings.')
    step('Under Personal Access Tokens, type a name (e.g. tabpull) → Create Token.')
    step('Copy the secret now: Tableau shows it only once.')
    note(
        'No Personal Access Tokens section? Your site admin has disabled them for your role.'
    )
    while True:
        print()
        settings = Settings(
            server,
            site,
            ask('TABLEAU_PAT_NAME', 'Token name:'),
            ask_secret('TABLEAU_PAT_SECRET', 'Token secret:'),
        )
        say('Checking the token…')
        try:
            with rest_session(settings):
                pass
        except (TableauError, OSError) as e:
            warn(f'Sign-in failed: {e}')
            if confirm('Try again?', default=True):
                continue
            _skipped.append(
                'PAT sign-in failed; saved anyway. Re-run the wizard once it works.'
            )
        else:
            say(f'{GREEN}✓ Signed in.{RESET}')
        write_env('TABLEAU_PAT_NAME', settings.pat_name)
        write_env('TABLEAU_PAT_SECRET', settings.pat_secret)
        return settings


def _sso_stage(settings: Settings) -> None:
    stage('Browser sign-in (SSO)')
    say(
        'Crosstabs of dashboard sheets run in a hidden browser using your normal SSO sign-in.'
    )
    say(
        'A browser window opens: sign in as usual (SSO, MFA). It closes itself once you are in.'
    )
    note(f'The session cookies are saved to {AUTH_STATE}. Keep that file private.')
    if not confirm('Sign in now?', default=True):
        _skipped.append(
            'Skipped SSO sign-in; crosstab.py opens the sign-in window when it needs it.'
        )
        return
    with sync_playwright() as pw:
        sso_login(pw, settings)
    pause()


def main() -> None:
    banner('tabpull setup')
    server, site = _site_stage()
    settings = _pat_stage(server, site)
    _sso_stage(settings)
    finish('uv run src/crosstab.py add')


if __name__ == '__main__':
    main()
