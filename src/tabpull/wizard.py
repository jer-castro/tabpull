import itertools
import platform
import shutil
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path

import questionary
from playwright.sync_api import sync_playwright
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from tableauserverclient.server.endpoint.exceptions import TableauError

from tabpull import ui
from tabpull.tableau import (
    Settings,
    check_site_name,
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


def banner(title: str) -> None:
    ui.console.clear()
    ui.console.print(
        Panel(
            f'[dim]{TOTAL_STAGES} stages. Each site keeps its own token and browser session.[/]',
            title=f'[bold]{escape(title)}[/]',
            title_align='left',
            border_style='blue',
        )
    )
    pause('Press Enter to start…')


def stage(name: str) -> None:
    number = next(_run.stages)
    ui.console.clear()
    ui.console.print(
        Rule(
            f'[dim]Stage {number} of {TOTAL_STAGES}[/]  [bold]{escape(name)}',
            align='left',
            style='blue',
        )
    )


def _line(markup: str) -> None:
    ui.console.print(f'  {markup}', soft_wrap=True)


def say(text: str) -> None:
    _line(escape(text))


def step(text: str) -> None:
    _line(f'[blue]•[/] {escape(text)}')


def note(text: str) -> None:
    _line(f'[dim]{escape(text)}[/]')


def warn(text: str) -> None:
    _line(f'[yellow]⚠ {escape(text)}[/]')


def open_url(url: str) -> None:
    say(f'Opening {url}')
    if 'microsoft' in platform.release().lower() and (
        opener := shutil.which('wslview') or shutil.which('explorer.exe')
    ):
        webbrowser.GenericBrowser(opener).open(url)
    elif not webbrowser.open(url):
        warn('Could not open a browser; open the URL above yourself.')


def pause(text: str = 'Press Enter when done…') -> None:
    ui.ask(questionary.press_any_key_to_continue(text, style=ui.STYLE))


def confirm(question: str, *, default: bool = False) -> bool:
    return bool(ui.ask(questionary.confirm(question, default=default, style=ui.STYLE)))


def _checked(check: Callable[[str], object]) -> Callable[[str], bool | str]:

    def validate(value: str) -> bool | str:
        try:
            check(value.strip())
        except ValueError as e:
            return str(e)
        return True

    return validate


def ask(key: str, prompt: str) -> str:
    existing = read_env_file(_run.env_path).get(key, '')
    answer = ui.ask(questionary.text(prompt, default=existing, style=ui.STYLE))
    return str(answer).strip() or existing


def ask_secret(key: str, prompt: str) -> str:
    existing = read_env_file(_run.env_path).get(key, '')
    instruction = '(Enter keeps the current one)' if existing else None
    answer = ui.ask(
        questionary.password(prompt, instruction=instruction, style=ui.STYLE)
    )
    return str(answer).strip() or existing


def write_env(key: str, value: str) -> None:
    upsert_env(_run.env_path, key, value)
    _run.written.append(key)


def finish(next_step: str) -> None:
    ui.console.clear()
    ui.console.print(Rule('[green bold]Done', align='left', style='green'))
    for key in dict.fromkeys(_run.written):
        _line(f'[green]✓[/] {key} → {escape(str(_run.env_path))}')
    for item in _run.skipped:
        warn(item)
    note(f'Jobs default to {jobs_path()}.')
    note('Exports go to the folder you run tabpull from, or --out.')
    _line(f'\n  Next: [bold]{escape(next_step)}[/]\n')


def _prompt_site_name() -> str:
    note('Jobs refer to this name. Letters, numbers, ".", "_" and "-".')
    note('The Tableau site content URL is a good name when you have one.')
    name = ui.ask(
        questionary.text(
            'Site name', validate=_checked(check_site_name), style=ui.STYLE
        )
    )
    return check_site_name(str(name).strip())


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
    validate = _checked(parse_tableau_url)
    while True:
        url = str(
            ui.ask(
                questionary.text(
                    'Dashboard URL',
                    validate=lambda value: (
                        (not value.strip() and bool(server)) or validate(value)
                    ),
                    style=ui.STYLE,
                )
            )
        ).strip()
        if not url:
            return name, server.rstrip('/'), env.get('TABLEAU_SITE', '')
        parsed = parse_tableau_url(url)
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
        'The token lets tabpull find views. Exporting a sheet uses the browser sign-in in the next step.'
    )
    open_url(home_url(server, site))
    step('Click your profile picture (top right) → My Account Settings.')
    step('Under Personal Access Tokens, type a name (e.g. tabpull) → Create Token.')
    step('Copy the secret now: Tableau shows it only once.')
    note(
        'No Personal Access Tokens section? Your site admin has disabled them for your role.'
    )
    while True:
        settings = Settings(
            server=server,
            site=site,
            pat_name=ask('TABLEAU_PAT_NAME', 'Token name'),
            name=name,
            auth_path=_run.auth_path,
            pat_secret=ask_secret('TABLEAU_PAT_SECRET', 'Token secret'),
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
            _line('[green]✓ Signed in.[/]')
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
    if not ui.interactive():
        msg = 'tabpull setup needs a terminal: it asks for a token and opens a browser.'
        raise SystemExit(msg)
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
