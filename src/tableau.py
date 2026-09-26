"""Tableau settings, PAT sign-in for the REST API, and the SSO browser session."""

import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import tableauserverclient as tsc
from playwright.sync_api import Browser, BrowserContext, Playwright
from playwright.sync_api import Error as PlaywrightError

ENV_FILE = Path('.env')
AUTH_STATE = Path('.auth/tableau-state.json')
SETTING_KEYS = (
    'TABLEAU_SERVER_URL',
    'TABLEAU_SITE',
    'TABLEAU_PAT_NAME',
    'TABLEAU_PAT_SECRET',
)
LOGIN_TIMEOUT_S = 300
LOGIN_POLL_MS = 2000
BROWSER_CHANNELS = ('chrome', 'msedge', None)

_SITE_RE = re.compile(r'^/+(?:t|site)/([^/?]+)')
_VIEW_RE = re.compile(r'/views/([^/?]+)/([^/?]+)')


class MissingSettingsError(Exception):
    def __init__(self, keys: list[str]) -> None:
        super().__init__(f'Missing {", ".join(keys)} in {ENV_FILE}')


def home_url(server: str, site: str) -> str:
    return f'{server}/#/site/{site}/home' if site else f'{server}/#/home'


@dataclass(frozen=True)
class Settings:
    server: str
    site: str
    pat_name: str
    pat_secret: str

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


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        key, sep, value = line.partition('=')
        if sep and not key.lstrip().startswith('#'):
            values[key.strip()] = value.strip().strip('"\'')
    return values


def load_settings() -> Settings:
    """Settings from the environment, falling back to `.env`."""
    values = read_env_file() | {
        key: os.environ[key] for key in SETTING_KEYS if key in os.environ
    }
    # TABLEAU_SITE is legitimately empty for a Tableau Server default site.
    missing = [
        key
        for key in SETTING_KEYS
        if key not in values or (not values[key] and key != 'TABLEAU_SITE')
    ]
    if missing:
        raise MissingSettingsError(missing)
    return Settings(
        server=values['TABLEAU_SERVER_URL'].rstrip('/'),
        site=values['TABLEAU_SITE'],
        pat_name=values['TABLEAU_PAT_NAME'],
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


def launch_browser(pw: Playwright, *, headless: bool) -> Browser:
    for channel in BROWSER_CHANNELS:
        try:
            return pw.chromium.launch(channel=channel, headless=headless)
        except PlaywrightError:
            continue
    msg = 'No Chrome or Edge found. Install one, or run: uv run playwright install chromium'
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
        storage_state=AUTH_STATE if AUTH_STATE.exists() else None, no_viewport=True
    )
    page = context.new_page()
    page.goto(settings.home_url)
    print(
        'Sign in to Tableau in the browser window (SSO, MFA, as usual). It closes by itself once you are in.'
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
    AUTH_STATE.parent.mkdir(exist_ok=True)
    context.storage_state(path=AUTH_STATE)
    AUTH_STATE.chmod(0o600)
    browser.close()
    print(f'Saved browser session to {AUTH_STATE}')


def browser_session(pw: Playwright, settings: Settings) -> BrowserContext:
    """Headless context with a live Tableau session, prompting for SSO when the saved one is missing or expired."""
    browser = launch_browser(pw, headless=True)
    if AUTH_STATE.exists():
        context = browser.new_context(storage_state=AUTH_STATE)
        if session_valid(context, settings):
            return context
        context.close()
    if not sys.stdin.isatty():
        msg = 'Tableau browser session is missing or expired. Run: uv run src/crosstab.py login'
        raise SystemExit(msg)
    sso_login(pw, settings)
    return browser.new_context(storage_state=AUTH_STATE)
