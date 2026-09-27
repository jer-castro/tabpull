"""Tableau settings, PAT sign-in for the REST API, and the SSO browser session.

The config directory holds site tokens, SSO cookies, and the jobs file. It is the XDG
config directory when `XDG_CONFIG_HOME` is set, and the usual OS folder otherwise.
Exports go to the working directory, so they are not managed here.
"""

import importlib.metadata
import os
import re
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import tableauserverclient as tsc
from playwright.sync_api import Browser, BrowserContext, Playwright
from playwright.sync_api import Error as PlaywrightError

SETTING_KEYS = (
    'TABLEAU_SERVER_URL',
    'TABLEAU_SITE',
    'TABLEAU_PAT_NAME',
    'TABLEAU_PAT_SECRET',
)
LOGIN_TIMEOUT_S = 300
LOGIN_POLL_MS = 2000
BROWSER_CHANNELS = ('chrome', 'msedge', None)
_APP = 'tabpull'
_SITE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')

_SITE_RE = re.compile(r'^/+(?:t|site)/([^/?]+)')
_VIEW_RE = re.compile(r'/views/([^/?]+)/([^/?]+)')


class MissingSettingsError(Exception):
    def __init__(self, keys: list[str], path: Path) -> None:
        self.keys = keys
        self.path = path
        super().__init__(
            f'Missing {", ".join(keys)} in {path}. Run: tabpull setup --site {path.stem}'
        )


class UnknownSiteError(Exception):
    pass


def check_site_name(name: str) -> str:
    if _SITE_NAME.fullmatch(name):
        return name
    msg = (
        f'Site name {name!r} should start with a letter or number and contain only '
        'letters, numbers, ".", "_" and "-".'
    )
    raise ValueError(msg)


def _windows_dir(env_var: str, fallback: Path) -> Path:
    value = os.environ.get(env_var)
    return Path(value) if value else fallback


def _rooted(env_var: str, unix_default: Path, windows: Path, mac: Path) -> Path:
    if override := os.environ.get(env_var):
        return Path(override) / _APP
    if sys.platform == 'win32':
        return windows / _APP
    if sys.platform == 'darwin':
        return mac / _APP
    return unix_default / _APP


def config_dir() -> Path:
    """Site tokens, SSO cookies, and the jobs file."""
    return _rooted(
        'XDG_CONFIG_HOME',
        Path.home() / '.config',
        _windows_dir('APPDATA', Path.home() / 'AppData' / 'Roaming'),
        Path.home() / 'Library' / 'Application Support',
    )


def data_dir() -> Path:
    """XDG data directory: `XDG_DATA_HOME`, else ~/.local/share on macOS and Linux, else LocalAppData.

    New exports are not stored here. Old auth cookies are at `_legacy_data_dir`.
    """
    share = Path.home() / '.local' / 'share'
    return _rooted(
        'XDG_DATA_HOME',
        share,
        _windows_dir('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'),
        share,
    )


def _legacy_data_dir() -> Path:
    """Where auth cookies lived before they moved next to the jobs file.

    macOS used Application Support, which is already the config directory. Other
    systems used the data directory. `XDG_DATA_HOME` was honored on every OS.
    """
    if os.environ.get('XDG_DATA_HOME'):
        return data_dir()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / _APP
    return data_dir()


def _move_dir(source: Path, dest: Path) -> None:
    """Move a directory once. Leave it in place when the destination already exists."""
    if source == dest or not source.is_dir() or dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, dest)


def _migrate_layout() -> None:
    """Move auth cookies into the config directory.

    Anything else in the old folder, including old exports, stays where it is.
    """
    _move_dir(_legacy_data_dir() / 'auth', config_dir() / 'auth')


def jobs_path() -> Path:
    return config_dir() / 'jobs.toml'


def site_env_path(name: str) -> Path:
    return config_dir() / 'sites' / f'{check_site_name(name)}.env'


def site_auth_path(name: str) -> Path:
    _migrate_layout()
    return config_dir() / 'auth' / f'{check_site_name(name)}.json'


def list_sites() -> list[str]:
    directory = config_dir() / 'sites'
    if not directory.is_dir():
        return []
    return sorted(
        path.stem for path in directory.glob('*.env') if _SITE_NAME.fullmatch(path.stem)
    )


def home_url(server: str, site: str) -> str:
    return f'{server}/#/site/{site}/home' if site else f'{server}/#/home'


@dataclass(frozen=True)
class Settings:
    server: str
    site: str
    pat_name: str
    name: str
    auth_path: Path
    pat_secret: str = field(repr=False)

    @property
    def home_url(self) -> str:
        return home_url(self.server, self.site)

    def view_url(self, view: str) -> str:
        """Embeddable URL for a `Workbook/View` path."""
        site = f'/t/{self.site}' if self.site else ''
        return f'{self.server}{site}/views/{view}'


@dataclass(frozen=True)
class TableauUrl:
    server: str
    site: str
    view: str | None


def parse_tableau_url(url: str) -> TableauUrl:
    """Split a browser URL (Cloud `#/site/..` or Server `/t/..` style) into server, site and view."""
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        msg = f'Not a URL: {url!r}'
        raise ValueError(msg)
    route = parts.path + parts.fragment
    site = _SITE_RE.search(route)
    view = _VIEW_RE.search(route)
    return TableauUrl(
        server=f'{parts.scheme}://{parts.netloc}',
        site=site.group(1) if site else '',
        view=f'{view.group(1)}/{view.group(2)}' if view else None,
    )


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        key, sep, value = line.partition('=')
        if sep and not key.lstrip().startswith('#'):
            values[key.strip()] = value.strip().strip('"\'')
    return values


def upsert_env(path: Path, key: str, value: str) -> None:
    """Upsert KEY=VALUE, keeping every other line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    kept = [line for line in lines if line.partition('=')[0].strip() != key]
    path.write_text('\n'.join([*kept, f'{key}={value}']) + '\n', encoding='utf-8')
    path.chmod(0o600)


def save_site(name: str, values: dict[str, str]) -> Path:
    """Write a site token file. Setup writes the same file one key at a time."""
    path = site_env_path(name)
    for key in SETTING_KEYS:
        if key in values:
            upsert_env(path, key, values[key])
    return path


def load_site(name: str) -> Settings:
    """Load one site's token file.

    The working directory and the process environment are not consulted, so each site
    keeps the token that setup saved for it.
    """
    path = site_env_path(name)
    if not path.is_file():
        known = ', '.join(list_sites()) or '(none)'
        msg = (
            f'No site named {name!r}. Configured sites: {known}. '
            f'Run: tabpull setup --site {name}'
        )
        raise UnknownSiteError(msg)
    values = read_env_file(path)
    # TABLEAU_SITE is legitimately empty for a Tableau Server default site.
    missing = [
        key
        for key in SETTING_KEYS
        if key not in values or (not values[key] and key != 'TABLEAU_SITE')
    ]
    if missing:
        raise MissingSettingsError(missing, path)
    return Settings(
        server=values['TABLEAU_SERVER_URL'].rstrip('/'),
        site=values['TABLEAU_SITE'],
        pat_name=values['TABLEAU_PAT_NAME'],
        name=name,
        auth_path=site_auth_path(name),
        pat_secret=values['TABLEAU_PAT_SECRET'],
    )


@contextmanager
def rest_session(settings: Settings) -> Iterator[tsc.Server]:
    """Signed-in REST API client; signs out on exit.

    Signing in with a PAT ends any other session using the same PAT, so don't share one token across parallel runs.
    """
    server = tsc.Server(settings.server, use_server_version=True)
    auth = tsc.PersonalAccessTokenAuth(
        settings.pat_name, settings.pat_secret, site_id=settings.site
    )
    with server.auth.sign_in(auth):
        yield server


def _chromium_install() -> str:
    """Install Chromium into the cache this Playwright version searches.

    `uvx playwright==<version>` uses the same browser revision and the default
    `ms-playwright` cache as the Playwright package installed with tabpull.
    """
    version = importlib.metadata.version('playwright')
    return f'uvx playwright=={version} install chromium'


def launch_browser(pw: Playwright, *, headless: bool) -> Browser:
    for channel in BROWSER_CHANNELS:
        try:
            return pw.chromium.launch(channel=channel, headless=headless)
        except PlaywrightError:
            continue
    msg = f'No Chrome or Edge found. Install one, or run: {_chromium_install()}'
    raise SystemExit(msg)


def session_valid(context: BrowserContext, settings: Settings) -> bool:
    """Ask Tableau whether the browser cookies still hold a live session.

    Uses getSessionInfo, the internal endpoint Tableau's own web client calls on every page load.
    """
    xsrf = next(
        (
            c['value']
            for c in context.cookies(settings.server)
            if c['name'] == 'XSRF-TOKEN'
        ),
        None,
    )
    if not xsrf:
        return False
    response = context.request.post(
        f'{settings.server}/vizportal/api/web/v1/getSessionInfo',
        data={'method': 'getSessionInfo', 'params': {}},
        headers={'X-XSRF-TOKEN': xsrf, 'Accept': 'application/json'},
        fail_on_status_code=False,
    )
    return response.ok


def sso_login(pw: Playwright, settings: Settings) -> None:
    """Open a real browser window, wait for the human to finish SSO, then save the cookies."""
    browser = launch_browser(pw, headless=False)
    context = browser.new_context(
        storage_state=settings.auth_path if settings.auth_path.exists() else None,
        no_viewport=True,
    )
    page = context.new_page()
    page.goto(settings.home_url)
    print(
        f'Sign in to {settings.server}, site {settings.site or "(default)"} '
        f'(local name {settings.name}). The window closes once you are in.'
    )
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    try:
        while not session_valid(context, settings):
            if time.monotonic() > deadline:
                msg = f'Gave up waiting for sign-in after {LOGIN_TIMEOUT_S}s.'
                raise SystemExit(msg)
            page.wait_for_timeout(LOGIN_POLL_MS)
    except PlaywrightError as e:
        msg = 'The browser closed before sign-in finished.'
        raise SystemExit(msg) from e
    settings.auth_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=settings.auth_path)
    settings.auth_path.chmod(0o600)
    browser.close()
    print(f'Saved browser session to {settings.auth_path}')


def browser_session(pw: Playwright, settings: Settings) -> BrowserContext:
    """Headless context with a live Tableau session, prompting for SSO when the saved one is missing or expired."""
    browser = launch_browser(pw, headless=True)
    if settings.auth_path.exists():
        context = browser.new_context(storage_state=settings.auth_path)
        if session_valid(context, settings):
            return context
        context.close()
    if not sys.stdin.isatty():
        msg = (
            f'Tableau browser session for site {settings.name!r} is missing or expired. '
            f'Run: tabpull login --site {settings.name}'
        )
        raise SystemExit(msg)
    sso_login(pw, settings)
    return browser.new_context(storage_state=settings.auth_path)
