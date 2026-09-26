"""Setup wizard for `tabpull setup`.

Asks for a local site name, a dashboard URL, a personal access token, and an SSO sign-in.
Safe to re-run: Enter keeps the current values for that site.
"""

import ctypes
import getpass
import itertools
import os
import platform
import shutil
import sys
import webbrowser
from pathlib import Path

from playwright.sync_api import sync_playwright
from tableauserverclient.server.endpoint.exceptions import TableauError

from tableau import (
    Settings,
    check_site_name,
    exports_dir,
    home_url,
    jobs_path,
    parse_tableau_url,
    read_env_file,
    rest_session,
    site_auth_path,
    site_env_path,
    sso_login,
    upsert_env,
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


class _Run:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.env_path = Path()
        self.auth_path = Path()
        self.written: list[str] = []
        self.skipped: list[str] = []
        self.stages = itertools.count(1)

    def bind(self, name: str) -> None:
        self.env_path = site_env_path(name)
        self.auth_path = site_auth_path(name)


_run = _Run()
TOTAL_STAGES = 3


def _clear() -> None:
    if sys.stdout.isatty():
        print('\033[2J\033[3J\033[H', end='')


def banner(title: str) -> None:
    _clear()
    print(
        f'\n  {BOLD}{title}{RESET}\n'
        f'  {DIM}{TOTAL_STAGES} stages. Each site keeps its own token and browser session.{RESET}\n'
    )
    pause('Press Enter to start…')


def stage(name: str) -> None:
    number = next(_run.stages)
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
    """Visible input; on re-runs Enter keeps the value already saved for this site."""
    existing = read_env_file(_run.env_path).get(key, '')
    suffix = f' {DIM}[{existing}]{RESET}' if existing else ''
    return input(f'  {prompt}{suffix} ').strip() or existing


def ask_secret(key: str, prompt: str) -> str:
    """Hidden input; on re-runs Enter keeps the value already saved for this site."""
    existing = read_env_file(_run.env_path).get(key, '')
    suffix = ' (Enter keeps the current one)' if existing else ''
    return getpass.getpass(f'  {prompt}{suffix} ').strip() or existing


def write_env(key: str, value: str) -> None:
    """Upsert KEY=VALUE into this site's file, keeping every other line."""
    upsert_env(_run.env_path, key, value)
    _run.written.append(key)


def finish(next_step: str) -> None:
    _clear()
    print(f'\n  {GREEN}{BOLD}Done.{RESET}\n')
    for key in dict.fromkeys(_run.written):
        print(f'  {GREEN}✓{RESET} {key} → {_run.env_path}')
    for item in _run.skipped:
        warn(item)
    note(f'Jobs default to {jobs_path()}.')
    note(f'Exports default to {exports_dir()}.')
    print(f'\n  Next: {BOLD}{next_step}{RESET}\n')


def _prompt_site_name() -> str:
    note('Jobs refer to this name. Letters, numbers, ".", "_" and "-".')
    note('The Tableau site content URL is a good name when you have one.')
    while True:
        name = input('\n  Site name: ').strip()
        try:
            return check_site_name(name)
        except ValueError as e:
            warn(str(e))


def _site_stage(preset: str | None) -> tuple[str, str, str]:
    stage('Tableau site')
    name = preset or _prompt_site_name()
    _run.bind(name)
    env = read_env_file(_run.env_path)
    server = env.get('TABLEAU_SERVER_URL', '')
    if server:
        note(
            f'Current: {server}, site {env.get("TABLEAU_SITE") or "(default)"}. Press Enter to keep it.'
        )
    step('In your usual browser, open any dashboard you normally crosstab from.')
    step('Copy the full URL from the address bar.')
    while True:
        url = input('\n  Paste the URL: ').strip()
        if not url and server:
            return name, server.rstrip('/'), env.get('TABLEAU_SITE', '')
        try:
            parsed = parse_tableau_url(url)
        except ValueError as e:
            warn(str(e))
            continue
        say(f'Server: {parsed.server}')
        say(f'Site:   {parsed.site or "(default site)"}')
        say(f'Name:   {name}')
        if confirm('Is that right?', default=True):
            write_env('TABLEAU_SERVER_URL', parsed.server)
            write_env('TABLEAU_SITE', parsed.site)
            return name, parsed.server, parsed.site


def _pat_stage(name: str, server: str, site: str) -> Settings:
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
            server=server,
            site=site,
            pat_name=ask('TABLEAU_PAT_NAME', 'Token name:'),
            name=name,
            auth_path=_run.auth_path,
            pat_secret=ask_secret('TABLEAU_PAT_SECRET', 'Token secret:'),
        )
        say('Checking the token…')
        try:
            with rest_session(settings):
                pass
        except (TableauError, OSError) as e:
            warn(f'Sign-in failed: {e}')
            if confirm('Try again?', default=True):
                continue
            _run.skipped.append(
                'PAT sign-in failed; saved anyway. Re-run tabpull setup once it works.'
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
    note(
        f'The session cookies are saved to {settings.auth_path}. Keep that file private.'
    )
    if not confirm('Sign in now?', default=True):
        _run.skipped.append(
            f'Skipped SSO sign-in. Run: tabpull login --site {settings.name}'
        )
        return
    with sync_playwright() as pw:
        sso_login(pw, settings)
    pause()


def main(site_name: str | None = None) -> str:
    if site_name is not None:
        try:
            check_site_name(site_name)
        except ValueError as e:
            raise SystemExit(str(e)) from e
    _run.reset()
    banner('tabpull setup')
    name, server, site = _site_stage(site_name)
    settings = _pat_stage(name, server, site)
    _sso_stage(settings)
    finish(f'tabpull add --site {name}')
    return name


if __name__ == '__main__':
    print('Run: tabpull setup', file=sys.stderr)
    sys.exit(2)
