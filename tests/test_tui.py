import asyncio
import os
import subprocess  # noqa: S404
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

if TYPE_CHECKING:
    from playwright.sync_api import Playwright

import pytest
from playwright.sync_api import Error as PlaywrightError
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import (
    Checkbox,
    DataTable,
    Input,
    OptionList,
    RadioButton,
    SelectionList,
    Static,
    TextArea,
)

import tabpull.tui.app as tui_app
from tabpull import embed, tableau, ui
from tabpull.jobs import Job, RangeFilter, ValuesFilter, load_jobs, save_jobs
from tabpull.run import Report
from tabpull.tui.app import _MARK, HomeScreen, JobScreen, TabpullApp
from tabpull.tui.forms import (
    ChecksForm,
    ChoiceScreen,
    ConfirmScreen,
    FilterForm,
    ParamForm,
    PickScreen,
    SetupForm,
    SheetsForm,
    TaskScreen,
    TextForm,
)


@pytest.fixture
def jobs_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    path = tmp_path / 'jobs.toml'
    save_jobs(
        path,
        [
            Job(
                'daily',
                'Sales/Overview',
                ['Totals', 'Detail'],
                'finance',
                [ValuesFilter('Region', ['West'], 'Totals')],
                {'Top N': '10'},
            ),
            Job('weekly', 'Sales/Overview', ['Totals'], 'finance'),
        ],
    )
    return path


def _drive(
    jobs_file: Path, steps: Callable[[TabpullApp, Pilot[None]], Awaitable[None]]
) -> None:
    async def go() -> None:
        tui = TabpullApp(jobs_file, jobs_file.parent / 'out')
        async with tui.run_test(size=(140, 45)) as pilot:
            await steps(tui, pilot)

    asyncio.run(go())


def _block_view(monkeypatch: pytest.MonkeyPatch, *, site: bool = False) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = 'the job screen opened the view'
        raise AssertionError(msg)

    monkeypatch.setattr('tabpull.tui.app.read_view', refuse)
    monkeypatch.setattr('tabpull.tui.app.sync_playwright', refuse)
    monkeypatch.setattr('tabpull.tui.app.browser_session', refuse)
    if site:
        monkeypatch.setattr('tabpull.tui.app.load_site', refuse)


def test_job_screen_names_change_field(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_view(monkeypatch)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter')
        sections = [str(widget.render()) for widget in tui.screen.query('.section')]
        assert sections == [
            'Filters  (n new, c change field, e/enter edit, d delete)',
            'Parameters  (n new, c change field, e/enter edit, d delete)',
        ]

    _drive(jobs_file, steps)


def test_home_lists_jobs_and_edits_a_filter_param_and_name(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_view(monkeypatch)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        assert isinstance(tui.screen, HomeScreen)
        assert tui.screen.query_one('#jobs', DataTable).row_count == 2

        await pilot.press('enter')
        assert isinstance(tui.screen, JobScreen)

        await pilot.press('e')
        assert isinstance(tui.screen, FilterForm)
        tui.screen.query_one('#values', Input).value = 'West|East'
        await pilot.press('ctrl+s')
        assert load_jobs(jobs_file)[0].filters == [
            ValuesFilter('Region', ['West', 'East'], 'Totals')
        ]

        await pilot.press('n')
        form = tui.screen
        assert isinstance(form, FilterForm)
        form.query_one('#field', Input).value = 'Order Date'
        form.query_one('#range-kind', RadioButton).value = True
        form.query_one('#low', Input).value = 'yesterday'
        await pilot.press('ctrl+s')
        assert tui.screen is form
        assert 'relative date' in str(form.query_one('#error', Static).render())
        form.query_one('#low', Input).value = '2026-09-01'
        await pilot.press('ctrl+s')
        assert load_jobs(jobs_file)[0].filters[1] == RangeFilter(
            'Order Date', 'Totals', '2026-09-01', None
        )

        await pilot.press('down', 'e')
        tui.screen.query_one('#high', Input).value = '12/31/2026'
        await pilot.press('ctrl+s', 'e')
        form = tui.screen
        assert isinstance(form, FilterForm)
        assert form.title_text == 'Edit filter Order Date'
        assert form.query_one('#high', Input).value == '12/31/2026'
        await pilot.press('escape', 'up')

        await pilot.press('tab', 'e')
        tui.screen.query_one('#value', Input).value = '25'
        await pilot.press('ctrl+s')
        assert load_jobs(jobs_file)[0].params == {'Top N': '25'}

        await pilot.press('s')
        tui.screen.query_one('#sheets', TextArea).text = 'Detail\n\nTotals\n'
        await pilot.press('ctrl+s')
        assert load_jobs(jobs_file)[0].sheets == ['Detail', 'Totals']

        await pilot.press('N')
        tui.screen.query_one('#value', Input).value = 'weekly'
        await pilot.press('ctrl+s')
        assert 'already exists' in str(tui.screen.query_one('#error', Static).render())
        tui.screen.query_one('#value', Input).value = 'daily-west'
        await pilot.press('ctrl+s')
        assert [job.name for job in load_jobs(jobs_file)] == ['daily-west', 'weekly']

        await pilot.press('escape')
        assert isinstance(tui.screen, HomeScreen)
        assert tui.screen.query_one('#jobs', DataTable).get_row_at(0)[1] == 'daily-west'

    _drive(jobs_file, steps)


def test_job_screen_changes_view_and_site_with_cli_validation(
    jobs_file: Path,
) -> None:
    tableau.save_site(
        'ops',
        {
            'TABLEAU_SERVER_URL': 'https://tableau.example',
            'TABLEAU_SITE': 'ops',
            'TABLEAU_PAT_NAME': 'tabpull',
            'TABLEAU_PAT_SECRET': 'pat-value',
        },
    )

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter', 'v')
        field = tui.screen.query_one('#value', Input)
        field.value = 'NoSlash'
        await pilot.press('ctrl+s')
        assert 'Workbook/View' in str(tui.screen.query_one('#error', Static).render())
        field.value = 'https://tableau.example/#/site/ops/views/Ops/Summary?:iid=1'
        await pilot.press('ctrl+s')
        assert load_jobs(jobs_file)[0].view == 'Ops/Summary'

        await pilot.press('S')
        field = tui.screen.query_one('#value', Input)
        field.value = 'nowhere'
        await pilot.press('ctrl+s')
        assert 'Configured sites: ops' in str(
            tui.screen.query_one('#error', Static).render()
        )
        field.value = 'ops'
        await pilot.press('ctrl+s')
        assert load_jobs(jobs_file)[0].site == 'ops'

    _drive(jobs_file, steps)


async def _until(pilot: Pilot[None], pred: Callable[[], bool]) -> None:
    for _ in range(50):
        await pilot.pause(0.05)
        if pred():
            await pilot.pause()
            return
    raise AssertionError(type(pilot.app.screen).__name__)


def _site(name: str = 'demo', *, site: str | None = None) -> None:
    tableau.save_site(
        name,
        {
            'TABLEAU_SERVER_URL': 'https://tableau.example',
            'TABLEAU_SITE': name if site is None else site,
            'TABLEAU_PAT_NAME': 'tabpull',
            'TABLEAU_PAT_SECRET': 'pat-value',
        },
    )


def _notes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    notes: list[str] = []
    monkeypatch.setattr(
        TabpullApp,
        'notify',
        lambda _self, message, **_kwargs: notes.append(str(message)),
    )
    return notes


class _Browser:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


def test_home_runs_marked_jobs_and_removes_after_confirm(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    release = threading.Event()

    def fake(jobs: list[Job], _out: Path, report: Report) -> list[str]:
        names = [job.name for job in jobs]
        seen.append(names)
        report.fail(jobs[0], 'nope')
        release.wait(5)
        return [names[0]]

    monkeypatch.setattr('tabpull.tui.app.export_jobs', fake)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('r')
        await _until(
            pilot,
            lambda: isinstance(tui.screen, TaskScreen) and bool(tui.screen.transcript),
        )
        assert isinstance(tui.screen, TaskScreen)
        assert any('nope' in line for line in tui.screen.transcript)
        release.set()
        await _until(
            pilot,
            lambda: (
                isinstance(tui.screen, TaskScreen)
                and any('failed' in line for line in tui.screen.transcript)
            ),
        )
        await pilot.press('escape')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))
        assert seen == [['daily']]
        assert 'FAILED' in tui.last_run
        assert isinstance(tui.screen, HomeScreen)

        release.clear()
        await pilot.press('space', 'space', 'r')
        await _until(pilot, lambda: isinstance(tui.screen, TaskScreen))
        release.set()
        await _until(
            pilot,
            lambda: (
                isinstance(tui.screen, TaskScreen)
                and any('failed' in line for line in tui.screen.transcript)
            ),
        )
        await pilot.press('escape')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))
        assert seen[-1] == ['daily', 'weekly']

        await pilot.press('d')
        assert isinstance(tui.screen, ConfirmScreen)
        await pilot.press('n')
        assert len(load_jobs(jobs_file)) == 2

        await pilot.press('space', 'd', 'y')
        assert [job.name for job in load_jobs(jobs_file)] == ['weekly']
        assert tui.screen.query_one('#jobs', DataTable).row_count == 1

        await pilot.press('a')
        assert isinstance(tui.screen, SetupForm)
        await pilot.press('escape')
        assert isinstance(tui.screen, HomeScreen)

    _drive(jobs_file, steps)


def test_run_stays_in_the_tui_when_export_crashes(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> list[str]:
        msg = 'browser blew up'
        raise RuntimeError(msg)

    monkeypatch.setattr('tabpull.tui.app.export_jobs', boom)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('r')
        await _until(
            pilot,
            lambda: (
                isinstance(tui.screen, TaskScreen)
                and any('browser blew up' in line for line in tui.screen.transcript)
            ),
        )
        assert isinstance(tui.screen, TaskScreen)
        assert 'press escape to close' in str(
            tui.screen.query_one('#task-hint', Static).render()
        )
        await pilot.press('escape')
        await _until(
            pilot,
            lambda: isinstance(tui.screen, HomeScreen) and 'FAILED' in tui.last_run,
        )
        assert 'browser blew up' in tui.last_run

    _drive(jobs_file, steps)


def test_escape_cancels_a_run_and_returns_home(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(*_args: object, **_kwargs: object) -> list[str]:
        while not ui.stopped():
            threading.Event().wait(0.02)
        return ['daily']

    monkeypatch.setattr('tabpull.tui.app.export_jobs', hang)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('r')
        await _until(pilot, lambda: isinstance(tui.screen, TaskScreen))
        await pilot.press('escape')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))
        assert 'cancelled' in tui.last_run

    _drive(jobs_file, steps)


def test_login_signs_in_on_one_site_and_picks_when_there_are_two(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)
    signed: list[str] = []
    monkeypatch.setattr(
        'tabpull.tui.app.sign_in',
        lambda settings: signed.append(settings.name) or settings.name,
    )
    _site('demo')

    async def one(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('l')
        await _until(
            pilot, lambda: signed == ['demo'] and isinstance(tui.screen, HomeScreen)
        )
        assert any('Signed in to demo' in note for note in notes)

    _drive(jobs_file, one)
    _site('other')
    signed.clear()

    async def two(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('l')
        await _until(pilot, lambda: isinstance(tui.screen, ChoiceScreen))
        await pilot.press('enter')
        await _until(
            pilot, lambda: signed == ['demo'] and isinstance(tui.screen, HomeScreen)
        )

    _drive(jobs_file, two)


def test_ctrl_c_during_login_returns_home(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)
    finished = threading.Event()

    def hang(_settings: object) -> str:
        try:
            while not ui.stopped():
                threading.Event().wait(0.02)
        finally:
            finished.set()
        return 'demo'

    monkeypatch.setattr('tabpull.tui.app.sign_in', hang)
    _site()

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('l')
        await _until(pilot, lambda: isinstance(tui.screen, TaskScreen))
        await pilot.press('ctrl+c')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))
        assert 'Cancelled.' in notes

    _drive(jobs_file, steps)
    assert finished.wait(2)


def test_login_without_a_site_stays_home(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('l')
        await pilot.pause()
        assert isinstance(tui.screen, HomeScreen)
        assert any('Press s' in note for note in notes)

    _drive(jobs_file, steps)


def test_setup_saves_a_site_from_the_form(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)
    signed: list[str] = []
    pasted = 'pat-value-2'
    monkeypatch.setattr(
        'tabpull.tui.forms.rest_session', lambda _settings: nullcontext()
    )
    monkeypatch.setattr('tabpull.tui.forms.sync_playwright', _Browser)
    monkeypatch.setattr(
        'tabpull.tui.forms.sso_login',
        lambda _pw, settings: signed.append(settings.name),
    )

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('s')
        form = tui.screen
        assert isinstance(form, SetupForm)
        form.query_one('#site', Input).value = 'finance'
        await pilot.press('ctrl+s')
        assert 'Dashboard URL is empty' in str(
            form.query_one('#error', Static).render()
        )
        form.query_one(
            '#url', Input
        ).value = 'https://tableau.example/#/site/finance/views/Sales/Overview'
        form.query_one('#pat-secret', Input).value = pasted
        await pilot.press('ctrl+s')
        await _until(
            pilot, lambda: isinstance(tui.screen, HomeScreen) and signed == ['finance']
        )
        saved = tableau.load_site('finance')
        assert saved.server == 'https://tableau.example'
        assert saved.site == 'finance'
        assert saved.pat_secret == pasted
        assert all(pasted not in note for note in notes)

    _drive(jobs_file, steps)


def test_add_walks_the_view_and_saves_a_job(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site()

    class Item:
        name = 'Overview'
        content_url = 'Sales/sheets/Overview'
        sheet_type = 'dashboard'

    info = {
        'sheets': [
            {
                'name': 'Order Detail',
                'filters': [
                    {'field': 'Region', 'type': 'categorical', 'current': 'West'}
                ],
            }
        ],
        'params': [{'name': 'Top N', 'current': '10'}],
    }
    monkeypatch.setattr('tabpull.tui.app.search_views', lambda *_a, **_k: [Item()])
    monkeypatch.setattr('tabpull.tui.app.sync_playwright', _Browser)
    monkeypatch.setattr('tabpull.tui.app.browser_session', lambda *_a, **_k: object())
    monkeypatch.setattr('tabpull.tui.app.read_view', lambda *_a, **_k: info)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('a')
        assert isinstance(tui.screen, TextForm)
        tui.screen.query_one('#value', Input).value = 'overview'
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, ChoiceScreen))
        await pilot.press('enter')
        await _until(pilot, lambda: isinstance(tui.screen, TextForm))
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, ChecksForm))
        await pilot.press('space', 'ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        await pilot.press('enter')
        await _until(pilot, lambda: isinstance(tui.screen, FilterForm))
        await pilot.press('ctrl+s')
        await _until(
            pilot,
            lambda: (
                isinstance(tui.screen, PickScreen)
                and tui.screen.title_text == 'Add a filter'
            ),
        )
        await pilot.press('down', 'enter')
        await _until(
            pilot,
            lambda: (
                isinstance(tui.screen, PickScreen)
                and tui.screen.title_text == 'Set a parameter'
            ),
        )
        await pilot.press('down', 'enter')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))
        job = load_jobs(jobs_file)[-1]
        assert job.name == 'Overview'
        assert job.site == 'demo'
        assert job.view == 'Sales/Overview'
        assert job.sheets == ['Order Detail']
        assert job.filters == [ValuesFilter('Region', ['West'], 'Order Detail')]
        assert job.params == {}

    _drive(jobs_file, steps)


def test_add_search_error_returns_home(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)
    _site()
    monkeypatch.setattr('tabpull.tui.app.search_views', lambda *_a, **_k: [])

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('a')
        tui.screen.query_one('#value', Input).value = 'missing'
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))
        assert any('No views' in note for note in notes)
        assert [job.name for job in load_jobs(jobs_file)] == ['daily', 'weekly']

    _drive(jobs_file, steps)


def test_sso_login_stops_when_cancelled(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site()
    settings = tableau.load_site('demo')
    closed = []

    class Page:
        def goto(self, _url: str) -> None:
            return None

        def wait_for_timeout(self, _ms: int) -> None:
            msg = 'should stop before waiting'
            raise AssertionError(msg)

    class Context:
        def new_page(self) -> Page:
            return Page()

        def cookies(self, _server: str) -> list[object]:
            return []

    class Browser:
        def new_context(self, **_kwargs: object) -> Context:
            return Context()

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(tableau, 'launch_browser', lambda *_a, **_k: Browser())
    monkeypatch.setattr(tableau, 'session_valid', lambda *_a, **_k: False)
    with (
        ui.capture(lambda _line: None, lambda: True),
        pytest.raises(SystemExit, match='cancelled'),
    ):
        tableau.sso_login(cast('Playwright', object()), settings)
    assert closed == [True]
    assert not settings.auth_path.exists()


def test_sign_in_completing_on_the_last_poll_saves_the_session(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site()
    settings = tableau.load_site('demo')
    clock = {'now': 1_000.0}
    signed_in = {'ok': False}
    polls = {'n': 0}
    events: list[str] = []

    def monotonic() -> float:
        return clock['now']

    class Page:
        def goto(self, _url: str) -> None:
            return None

        def wait_for_timeout(self, _ms: int) -> None:
            polls['n'] += 1
            assert polls['n'] == 1
            clock['now'] += tableau.LOGIN_TIMEOUT_S + 1
            signed_in['ok'] = True

    class Context:
        def new_page(self) -> Page:
            return Page()

        def storage_state(self, path: Path) -> None:
            Path(path).write_text('session', encoding='utf-8')
            events.append('save')

    class Browser:
        def new_context(self, **_kwargs: object) -> Context:
            return Context()

        def close(self) -> None:
            events.append('close')

    monkeypatch.setattr(tableau.time, 'monotonic', monotonic)
    monkeypatch.setattr(tableau, 'launch_browser', lambda *_a, **_k: Browser())
    monkeypatch.setattr(tableau, 'session_valid', lambda *_a, **_k: signed_in['ok'])
    notes: list[str] = []
    with ui.capture(notes.append):
        tableau.sso_login(cast('Playwright', object()), settings)

    assert polls['n'] == 1
    assert events == ['save', 'close']
    assert settings.auth_path.read_text(encoding='utf-8') == 'session'
    assert notes[-1] == f'Saved browser session to {settings.auth_path}'


class _Cdp:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def send(self, method: str) -> dict[str, list[dict[str, object]]]:
        assert method == 'SystemInfo.getProcessInfo'
        return {
            'processInfo': [
                {'type': 'renderer', 'id': 1},
                {'type': 'browser', 'id': self.pid},
            ]
        }

    def detach(self) -> None:
        return None


class _Launcher:
    def __init__(self, browser: object, pid: int) -> None:
        vars(browser)['new_browser_cdp_session'] = lambda: _Cdp(pid)
        vars(browser)['on'] = lambda *_args: None
        self.browser = browser
        self.chromium = self

    def launch(self, **_kwargs: object) -> object:
        return self.browser

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])


def test_cancel_during_sign_in_request_does_not_wait_it_out(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site()
    settings = tableau.load_site('demo')
    proc = _sleeper()
    entered = threading.Event()
    stop = {'on': False}

    class Request:
        def post(self, *_args: object, **_kwargs: object) -> object:
            entered.set()
            proc.wait()
            msg = 'Target page, context or browser has been closed'
            raise PlaywrightError(msg)

    class Page:
        def goto(self, _url: str) -> None:
            return None

        def wait_for_timeout(self, _ms: int) -> None:
            msg = 'should stop during the request, not after it'
            raise AssertionError(msg)

    class Context:
        request = Request()

        def new_page(self) -> Page:
            return Page()

        def cookies(self, _server: str) -> list[dict[str, str]]:
            return [{'name': 'XSRF-TOKEN', 'value': 'token'}]

    class Browser:
        def new_context(self, **_kwargs: object) -> Context:
            return Context()

        def close(self) -> None:
            return None

    def flip() -> None:
        assert entered.wait(5)
        stop['on'] = True

    launcher = _Launcher(Browser(), proc.pid)
    threading.Thread(target=flip, daemon=True).start()
    started = time.monotonic()
    try:
        with (
            ui.capture(lambda _line: None, lambda: stop['on']),
            pytest.raises(SystemExit, match='cancelled'),
        ):
            tableau.sso_login(cast('Playwright', launcher), settings)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)

    assert time.monotonic() - started < 3
    assert proc.poll() is not None
    assert not settings.auth_path.exists()


def test_ctrl_c_stops_sign_in_during_the_poll(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)
    _site()
    proc = _sleeper()
    entered = threading.Event()

    class Page:
        def goto(self, _url: str) -> None:
            return None

        def wait_for_timeout(self, _ms: int) -> None:
            entered.set()
            proc.wait()
            msg = 'Target page, context or browser has been closed'
            raise PlaywrightError(msg)

    class Context:
        def new_page(self) -> Page:
            return Page()

        def cookies(self, _server: str) -> list[object]:
            return []

    class Browser:
        def new_context(self, **_kwargs: object) -> Context:
            return Context()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        'tabpull.tui.forms.sync_playwright', lambda: _Launcher(Browser(), proc.pid)
    )

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('l')
        await _until(pilot, entered.is_set)
        await pilot.press('ctrl+c')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))

    try:
        _drive(jobs_file, steps)
        assert proc.wait(timeout=2) is not None or proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
    assert any('cancel' in note.lower() for note in notes)


def test_escape_stops_the_sheet_being_exported(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site('finance')
    proc = _sleeper()
    entered = threading.Event()

    class Page:
        def evaluate(self, script: str, _arg: object = None) -> None:
            if script == embed.EXPORT_JS:
                entered.set()
                proc.wait()
                msg = 'Target page, context or browser has been closed'
                raise PlaywrightError(msg)

        def expect_download(self, timeout: int) -> object:
            class Expect:
                def __enter__(self) -> object:
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

            return Expect()

        def close(self) -> None:
            return None

    def browser_session(pw: 'Playwright', _settings: object) -> object:
        tableau.launch_browser(pw, headless=True)
        return object()

    monkeypatch.setattr(
        'tabpull.run.sync_playwright',
        lambda: _Launcher(type('Browser', (), {})(), proc.pid),
    )
    monkeypatch.setattr('tabpull.run.browser_session', browser_session)
    monkeypatch.setattr('tabpull.embed.open_view', lambda *_a, **_k: Page())

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('r')
        await _until(pilot, entered.is_set)
        await pilot.press('escape')
        await _until(
            pilot,
            lambda: isinstance(tui.screen, HomeScreen) and 'cancelled' in tui.last_run,
        )
        assert not (jobs_file.parent / 'out' / 'daily' / 'Totals.csv').exists()

    try:
        _drive(jobs_file, steps)
        assert proc.wait(timeout=2) is not None or proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_setup_blank_fields_keep_the_saved_site(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tableau.save_site(
        'finance',
        {
            'TABLEAU_SERVER_URL': 'https://tableau.example',
            'TABLEAU_SITE': 'sales',
            'TABLEAU_PAT_NAME': 'mytoken',
            'TABLEAU_PAT_SECRET': 'old-secret',
        },
    )
    monkeypatch.setattr(
        'tabpull.tui.forms.rest_session', lambda _settings: nullcontext()
    )

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('s')
        form = tui.screen
        assert isinstance(form, SetupForm)
        form.query_one('#site', Input).value = 'finance'
        form.query_one('#sso', Checkbox).value = False
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))
        saved = tableau.load_site('finance')
        assert saved.server == 'https://tableau.example'
        assert saved.site == 'sales'
        assert saved.pat_name == 'mytoken'
        assert saved.pat_secret == 'old-secret'  # noqa: S105

    _drive(jobs_file, steps)


def _painted(widget: Widget) -> str:
    return '\n'.join(
        ''.join(segment.text for segment in widget.render_line(y))
        for y in range(widget.size.height)
    )


def test_setup_save_anyway_and_a_refused_check_stays_on_the_form(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_settings: object) -> None:
        msg = (
            "HTTPConnectionPool(host='127.0.0.1', port=9): Max retries exceeded "
            'with url: /api/3.26/auth/signin (Caused by NewConnectionError('
            "'<urllib3.connection.HTTPConnection object at 0x10>: "
            'Failed to establish a new connection: [Errno 61] Connection refused'
            '))'
        )
        raise RuntimeError(msg)

    monkeypatch.setattr('tabpull.tui.forms.rest_session', boom)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('s')
        form = tui.screen
        assert isinstance(form, SetupForm)
        form.query_one('#site', Input).value = 'finance'
        form.query_one('#url', Input).value = 'https://tableau.example'
        form.query_one('#pat-secret', Input).value = 'secret-value'
        form.query_one('#sso', Checkbox).value = False
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, ConfirmScreen))
        title = tui.screen.query_one('.title')
        assert title.region.right <= tui.size.width
        assert title.region.bottom <= tui.size.height
        assert 'Save this token anyway?' in _painted(title)
        yes = tui.screen.query_one('#yes')
        assert yes.region.bottom <= tui.size.height
        await pilot.press('n')
        assert isinstance(tui.screen, SetupForm)
        assert 'finance' not in tableau.list_sites()
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, ConfirmScreen))
        await pilot.press('y')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))
        assert tableau.load_site('finance').pat_secret == 'secret-value'  # noqa: S105

    _drive(jobs_file, steps)


def test_setup_save_error_stays_on_the_form(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        'tabpull.tui.forms.rest_session', lambda _settings: nullcontext()
    )

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = 'disk full'
        raise OSError(msg)

    monkeypatch.setattr('tabpull.tui.forms.save_site', boom)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('s')
        form = tui.screen
        assert isinstance(form, SetupForm)
        form.query_one('#site', Input).value = 'finance'
        form.query_one('#url', Input).value = 'https://tableau.example'
        form.query_one('#pat-secret', Input).value = 'secret-value'
        form.query_one('#sso', Checkbox).value = False
        await pilot.press('ctrl+s')
        await _until(
            pilot,
            lambda: 'disk full' in str(form.query_one('#error', Static).render()),
        )
        assert isinstance(tui.screen, SetupForm)
        assert 'finance' not in tableau.list_sites()

    _drive(jobs_file, steps)


def test_add_rejects_a_duplicate_name(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _site()

    class Item:
        name = 'Overview'
        content_url = 'Sales/sheets/Overview'
        sheet_type = 'dashboard'

    monkeypatch.setattr('tabpull.tui.app.search_views', lambda *_a, **_k: [Item()])

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('a')
        tui.screen.query_one('#value', Input).value = 'overview'
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, ChoiceScreen))
        await pilot.press('enter')
        await _until(pilot, lambda: isinstance(tui.screen, TextForm))
        tui.screen.query_one('#value', Input).value = 'daily'
        await pilot.press('ctrl+s')
        assert isinstance(tui.screen, TextForm)
        assert 'already exists' in str(tui.screen.query_one('#error', Static).render())
        assert [job.name for job in load_jobs(jobs_file)] == ['daily', 'weekly']

    _drive(jobs_file, steps)


def _fill_new_site(form: SetupForm, *, sso: bool) -> None:
    form.query_one('#site', Input).value = 'finance'
    form.query_one('#url', Input).value = 'https://tableau.example'
    form.query_one('#pat-secret', Input).value = 'secret-value'
    form.query_one('#sso', Checkbox).value = sso


def test_add_with_no_site_continues_after_setup(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        'tabpull.tui.forms.rest_session', lambda _settings: nullcontext()
    )

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('a')
        assert isinstance(tui.screen, SetupForm)
        _fill_new_site(tui.screen, sso=False)
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, TextForm))

    _drive(jobs_file, steps)


def test_add_continues_when_setup_sign_in_fails(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)
    monkeypatch.setattr(
        'tabpull.tui.forms.rest_session', lambda _settings: nullcontext()
    )

    def fail(_pw: object, _settings: object) -> None:
        msg = 'browser closed'
        raise SystemExit(msg)

    monkeypatch.setattr('tabpull.tui.forms.sso_login', fail)
    monkeypatch.setattr('tabpull.tui.forms.sync_playwright', _Browser)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('a')
        assert isinstance(tui.screen, SetupForm)
        _fill_new_site(tui.screen, sso=True)
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, TextForm))
        assert any('sign-in failed' in note for note in notes)
        assert tableau.load_site('finance').pat_secret == 'secret-value'  # noqa: S105

    _drive(jobs_file, steps)


def test_cancel_then_run_does_not_overlap(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)
    state = {'n': 0, 'peak': 0}
    lock = threading.Lock()
    release = threading.Event()

    def fake(jobs: list[Job], _out: Path, _report: Report) -> list[str]:
        with lock:
            state['n'] += 1
            state['peak'] = max(state['peak'], state['n'])
        release.wait(5)
        with lock:
            state['n'] -= 1
        return [job.name for job in jobs]

    monkeypatch.setattr('tabpull.tui.app.export_jobs', fake)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('r')
        await _until(
            pilot, lambda: isinstance(tui.screen, TaskScreen) and state['n'] == 1
        )
        await pilot.press('escape')
        await _until(pilot, lambda: isinstance(tui.screen, HomeScreen))
        await pilot.press('r')
        await pilot.pause()
        assert state['peak'] == 1
        assert state['n'] == 1
        assert isinstance(tui.screen, HomeScreen)
        assert any('Still stopping' in note for note in notes)
        release.set()
        await _until(pilot, lambda: state['n'] == 0)

    _drive(jobs_file, steps)


def test_second_q_returns_while_a_cancelled_search_is_blocked(tmp_path: Path) -> None:
    env = os.environ.copy()
    env['XDG_CONFIG_HOME'] = str(tmp_path / 'config')
    env['TABPULL_QUIT_ROOT'] = str(tmp_path)
    env['PYTHONUNBUFFERED'] = '1'
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, __file__, '--force-quit-child'],
            env=env,
            timeout=8,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = 'q twice did not return to the shell'
        raise AssertionError(msg) from exc
    assert proc.returncode == 0, proc.stderr
    assert 'force-quit-returned' in proc.stdout


def _plain(prompt: object) -> str:
    plain = getattr(prompt, 'plain', None)
    return plain if isinstance(plain, str) else str(prompt)


def _prompts(screen: Widget) -> list[str]:
    widget = screen.query_one(OptionList)
    return [
        _plain(widget.get_option_at_index(index).prompt)
        for index in range(widget.option_count)
    ]


async def _choose(pilot: Pilot[None], prompt: str) -> None:
    widget = pilot.app.screen.query_one(OptionList)
    found = next(
        (
            index
            for index in range(widget.option_count)
            if _plain(widget.get_option_at_index(index).prompt) == prompt
        ),
        None,
    )
    if found is None:
        raise AssertionError(prompt)
    widget.highlighted = found
    await pilot.pause()
    await pilot.press('enter')


def _sheet_rows(screen: Widget) -> list[tuple[str, str]]:
    widget = screen.query_one(SelectionList)
    rows: list[tuple[str, str]] = []
    for index in range(widget.option_count):
        option = widget.get_option_at_index(index)
        rows.append((_plain(option.prompt), str(getattr(option, 'value', ''))))
    return rows


def _rendered_option_lines(screen: Widget) -> list[str]:
    widget = screen.query_one(SelectionList)
    widget.render_lines(widget.content_region)
    return [widget.render_line(index).text for index in range(widget.option_count)]


def _fake_settings() -> tableau.Settings:
    return tableau.Settings(
        server='https://tableau.example',
        site='finance',
        pat_name='tabpull',
        name='finance',
        auth_path=Path('auth.json'),
        pat_secret='secret',  # noqa: S106
    )


def _view(
    sheets: list[tuple[str, list[tuple[str, str, str]]]],
    params: list[tuple[str, str]],
) -> dict[str, object]:
    return {
        'sheets': [
            {
                'name': name,
                'filters': [
                    {'field': field, 'type': kind, 'current': current}
                    for field, kind, current in filters
                ],
            }
            for name, filters in sheets
        ],
        'params': [{'name': name, 'current': current} for name, current in params],
    }


def _patch_view(
    monkeypatch: pytest.MonkeyPatch,
    info: dict[str, object],
    *,
    fail: str | None = None,
    hold: threading.Event | None = None,
) -> tuple[list[str], list[int]]:
    seen: list[str] = []
    opened: list[int] = []

    class _Opened:
        def __enter__(self) -> object:
            opened.append(1)
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    def read_view(_context: object, _settings: object, view: str) -> dict[str, object]:
        seen.append(view)
        if hold is not None and len(seen) == 1 and not hold.wait(5):
            msg = 'timed out waiting to open the view'
            raise SystemExit(msg)
        if fail is not None:
            raise SystemExit(fail)
        return info

    monkeypatch.setattr('tabpull.tui.app.read_view', read_view)
    monkeypatch.setattr('tabpull.tui.app.sync_playwright', _Opened)
    monkeypatch.setattr('tabpull.tui.app.browser_session', lambda *_a, **_k: object())
    monkeypatch.setattr('tabpull.tui.app.load_site', lambda _name: _fake_settings())
    return seen, opened


def _save_daily(jobs_file: Path, job: Job) -> None:
    save_jobs(jobs_file, [job])


def test_n_edits_a_saved_filter_appends_an_unsaved_one_and_keeps_other_rows(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_daily(
        jobs_file,
        Job(
            'daily',
            'Sales/Overview',
            ['Totals', 'Detail'],
            'finance',
            [
                ValuesFilter('Region', ['West'], 'Totals'),
                ValuesFilter('Legacy', ['Keep'], 'Totals'),
            ],
            {'Top N': '10', 'Legacy Param': '1'},
        ),
    )
    info = _view(
        [
            (
                'Totals',
                [
                    ('Region', 'categorical', 'West'),
                    ('Ship Mode', 'categorical', 'Standard'),
                ],
            )
        ],
        [('Top N', '99')],
    )
    hold = threading.Event()
    seen, opened = _patch_view(monkeypatch, info, hold=hold)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter', 'e')
        assert isinstance(tui.screen, FilterForm)
        await pilot.press('escape')
        assert seen == []

        await pilot.press('n')
        await _until(pilot, lambda: isinstance(tui.screen, TaskScreen))
        assert isinstance(tui.screen, TaskScreen)
        assert tui.screen.title_text == 'Opening Sales/Overview…'
        hold.set()
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        assert _prompts(tui.screen) == [
            f'{_MARK} Region  on Totals  = West',
            'Ship Mode  on Totals',
            'Done',
        ]
        await _choose(pilot, f'{_MARK} Region  on Totals  = West')
        await _until(pilot, lambda: isinstance(tui.screen, FilterForm))
        form = tui.screen
        assert isinstance(form, FilterForm)
        assert form.title_text == 'Edit filter Region'
        assert form.query_one('#values', Input).value == 'West'
        form.query_one('#values', Input).value = 'West|East'
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        await pilot.press('escape')
        assert isinstance(tui.screen, JobScreen)

        await pilot.press('n')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        assert seen == ['Sales/Overview']
        assert opened == [1]
        await _choose(pilot, 'Ship Mode  on Totals')
        await _until(pilot, lambda: isinstance(tui.screen, FilterForm))
        form = tui.screen
        assert isinstance(form, FilterForm)
        assert form.query_one('#field', Input).value == 'Ship Mode'
        assert form.query_one('#sheet', Input).value == 'Totals'
        assert form.query_one('#values', Input).value == 'Standard'
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))

        job = load_jobs(jobs_file)[0]
        assert job.filters == [
            ValuesFilter('Region', ['West', 'East'], 'Totals'),
            ValuesFilter('Legacy', ['Keep'], 'Totals'),
            ValuesFilter('Ship Mode', ['Standard'], 'Totals'),
        ]
        assert job.params == {'Top N': '10', 'Legacy Param': '1'}
        assert seen == ['Sales/Overview']

    _drive(jobs_file, steps)


def test_c_refuses_a_saved_field_and_replaces_a_row_with_the_live_default(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = [
        ValuesFilter('Region', ['West'], 'Totals'),
        ValuesFilter('Category', ['Furniture'], 'Totals'),
    ]
    _save_daily(
        jobs_file,
        Job('daily', 'Sales/Overview', ['Totals'], 'finance', list(original), {}),
    )
    info = _view(
        [
            (
                'Totals',
                [
                    ('Region', 'categorical', 'West'),
                    ('Category', 'categorical', 'Office'),
                    ('Ship Mode', 'categorical', 'Standard'),
                ],
            )
        ],
        [],
    )
    seen, _opened = _patch_view(monkeypatch, info)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter', 'c')
        await _until(pilot, lambda: isinstance(tui.screen, ChoiceScreen))
        assert isinstance(tui.screen, ChoiceScreen)
        assert tui.screen.title_text == 'Change field of Region'
        assert _prompts(tui.screen) == [
            f'{_MARK} Category  on Totals  = Furniture',
            'Ship Mode  on Totals',
        ]
        await _choose(pilot, f'{_MARK} Category  on Totals  = Furniture')
        await _until(pilot, lambda: isinstance(tui.screen, FilterForm))
        form = tui.screen
        assert isinstance(form, FilterForm)
        assert form.query_one('#field', Input).value == 'Category'
        assert form.query_one('#values', Input).value == 'Office'
        await pilot.press('ctrl+s')
        assert tui.screen is form
        assert (
            str(form.query_one('#error', Static).render())
            == 'filter Category on Totals is already on this job; edit that row'
        )
        assert load_jobs(jobs_file)[0].filters == original

        await pilot.press('escape', 'c')
        await _until(pilot, lambda: isinstance(tui.screen, ChoiceScreen))
        assert seen == ['Sales/Overview']
        await _choose(pilot, 'Ship Mode  on Totals')
        await _until(pilot, lambda: isinstance(tui.screen, FilterForm))
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, JobScreen))
        assert load_jobs(jobs_file)[0].filters == [
            ValuesFilter('Ship Mode', ['Standard'], 'Totals'),
            ValuesFilter('Category', ['Furniture'], 'Totals'),
        ]

    _drive(jobs_file, steps)


def test_sheetless_saved_filter_matches_the_first_sheet(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_daily(
        jobs_file,
        Job(
            'daily',
            'Sales/Overview',
            ['Totals', 'Detail'],
            'finance',
            [ValuesFilter('Region', ['West'])],
            {},
        ),
    )
    info = _view(
        [
            (
                'Totals',
                [('Region', 'categorical', 'Central')],
            ),
            (
                'Detail',
                [('Region', 'categorical', 'East')],
            ),
        ],
        [],
    )
    _patch_view(monkeypatch, info)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter', 'n')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        assert _prompts(tui.screen)[0] == f'{_MARK} Region  on Totals  = West'
        await _choose(pilot, f'{_MARK} Region  on Totals  = West')
        await _until(pilot, lambda: isinstance(tui.screen, FilterForm))
        form = tui.screen
        assert isinstance(form, FilterForm)
        assert not form.query_one('#sheet', Input).value
        assert form.query_one('#values', Input).value == 'West'
        form.query_one('#values', Input).value = 'East'
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        assert load_jobs(jobs_file)[0].filters == [ValuesFilter('Region', ['East'], '')]

    _drive(jobs_file, steps)


def test_parameters_edit_in_place_append_live_current_and_refuse_a_set_name(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_daily(
        jobs_file,
        Job(
            'daily',
            'Sales/Overview',
            ['Totals'],
            'finance',
            [],
            {'Top N': '10', 'Profit Bin Size': '5'},
        ),
    )
    info = _view(
        [('Totals', [])],
        [('Top N', '99'), ('Profit Bin Size', '8'), ('Bonus', '3')],
    )
    seen, _opened = _patch_view(monkeypatch, info)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter', 'tab', 'n')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        assert isinstance(tui.screen, PickScreen)
        assert tui.screen.title_text == 'Set a parameter'
        assert _prompts(tui.screen) == [
            f'{_MARK} Top N  = 10',
            f'{_MARK} Profit Bin Size  = 5',
            'Bonus',
            'Done',
        ]
        await _choose(pilot, f'{_MARK} Top N  = 10')
        await _until(pilot, lambda: isinstance(tui.screen, ParamForm))
        form = tui.screen
        assert isinstance(form, ParamForm)
        assert form.title_text == 'Edit parameter Top N'
        assert form.query_one('#value', Input).value == '10'
        form.query_one('#value', Input).value = '25'
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        await pilot.press('escape', 'n')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        assert seen == ['Sales/Overview']
        await _choose(pilot, 'Bonus')
        await _until(pilot, lambda: isinstance(tui.screen, ParamForm))
        form = tui.screen
        assert isinstance(form, ParamForm)
        assert form.title_text == 'Bonus'
        assert form.query_one('#name', Input).value == 'Bonus'
        assert form.query_one('#value', Input).value == '3'
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, PickScreen))
        await pilot.press('escape', 'c')
        await _until(pilot, lambda: isinstance(tui.screen, ChoiceScreen))
        assert _prompts(tui.screen) == [
            f'{_MARK} Profit Bin Size  = 5',
            f'{_MARK} Bonus  = 3',
        ]
        await _choose(pilot, f'{_MARK} Profit Bin Size  = 5')
        await _until(pilot, lambda: isinstance(tui.screen, ParamForm))
        form = tui.screen
        assert isinstance(form, ParamForm)
        assert form.query_one('#name', Input).value == 'Profit Bin Size'
        await pilot.press('ctrl+s')
        assert tui.screen is form
        assert 'already set' in str(form.query_one('#error', Static).render())
        assert load_jobs(jobs_file)[0].params == {
            'Top N': '25',
            'Profit Bin Size': '5',
            'Bonus': '3',
        }

    _drive(jobs_file, steps)


def test_failed_view_read_opens_a_blank_filter_form_until_the_view_changes(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)
    login = 'Run: tabpull login --site finance'
    hold = threading.Event()
    seen, opened = _patch_view(monkeypatch, _view([], []), fail=login, hold=hold)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter')
        assert seen == []
        await pilot.press('n')
        await _until(pilot, lambda: isinstance(tui.screen, TaskScreen))
        assert isinstance(tui.screen, TaskScreen)
        assert tui.screen.title_text == 'Opening Sales/Overview…'
        hold.set()
        await _until(pilot, lambda: isinstance(tui.screen, FilterForm))
        form = tui.screen
        assert isinstance(form, FilterForm)
        assert form.title_text == 'New filter'
        assert not form.query_one('#field', Input).value
        assert notes == [login]
        assert seen == ['Sales/Overview']
        assert opened == [1]

        await pilot.press('escape', 'n')
        await _until(pilot, lambda: isinstance(tui.screen, FilterForm))
        assert not isinstance(tui.screen, TaskScreen)
        assert seen == ['Sales/Overview']
        assert opened == [1]
        assert notes == [login, login]

        await pilot.press('escape', 'v')
        tui.screen.query_one('#value', Input).value = 'Sales/Other'
        await pilot.press('ctrl+s', 'n')
        await _until(pilot, lambda: isinstance(tui.screen, FilterForm))
        assert seen == ['Sales/Overview', 'Sales/Other']
        assert opened == [1, 1]

    _drive(jobs_file, steps)


def test_identical_filter_rows_can_still_be_edited(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_daily(
        jobs_file,
        Job(
            'daily',
            'Sales/Overview',
            ['Totals'],
            'finance',
            [
                ValuesFilter('Region', ['West'], 'Totals'),
                ValuesFilter('Region', ['West'], 'Totals'),
            ],
            {},
        ),
    )

    _block_view(monkeypatch, site=True)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter', 'e')
        assert isinstance(tui.screen, FilterForm)
        tui.screen.query_one('#values', Input).value = 'East'
        await pilot.press('ctrl+s')
        assert load_jobs(jobs_file)[0].filters == [
            ValuesFilter('Region', ['East'], 'Totals'),
            ValuesFilter('Region', ['West'], 'Totals'),
        ]

    _drive(jobs_file, steps)


def test_s_ticks_saved_sheets_including_one_missing_from_the_view(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_daily(
        jobs_file,
        Job(
            'daily',
            'Sales/Overview',
            ['Totals', 'Gone', 'Detail'],
            'finance',
            [],
            {},
        ),
    )
    info = _view(
        [('Detail', []), ('Totals', []), ('Extra', [])],
        [],
    )
    seen, _opened = _patch_view(monkeypatch, info)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter')
        assert seen == []
        await pilot.press('s')
        await _until(pilot, lambda: isinstance(tui.screen, ChecksForm))
        assert isinstance(tui.screen, ChecksForm)
        assert _sheet_rows(tui.screen) == [
            ('Totals', 'Totals'),
            ('Gone (not on the live view)', 'Gone'),
            ('Detail', 'Detail'),
            ('Extra', 'Extra'),
        ]
        checks = tui.screen.query_one(SelectionList)
        assert list(checks.selected) == ['Totals', 'Gone', 'Detail']
        await pilot.press('ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, JobScreen))
        assert load_jobs(jobs_file)[0].sheets == ['Totals', 'Gone', 'Detail']
        assert seen == ['Sales/Overview']

    _drive(jobs_file, steps)


def test_sheet_checklist_shows_bracketed_names_literally(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_daily(
        jobs_file,
        Job(
            'daily',
            'Sales/Overview',
            ['Overview [copy]', 'Overview', 'Sheet [old]'],
            'finance',
            [],
            {},
        ),
    )
    info = _view(
        [('Overview', []), ('Overview [copy]', []), ('Map [hidden]', [])],
        [],
    )
    _patch_view(monkeypatch, info)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter', 's')
        await _until(pilot, lambda: isinstance(tui.screen, ChecksForm))
        lines = _rendered_option_lines(tui.screen)
        assert 'Overview [copy]' in lines[0]
        assert 'Overview' in lines[1]
        assert '[copy]' not in lines[1]
        assert 'Sheet [old] (not on the live view)' in lines[2]
        assert 'Map [hidden]' in lines[3]
        checks = tui.screen.query_one(SelectionList)
        checks.highlighted = 0
        await pilot.press('space', 'ctrl+s')
        await _until(pilot, lambda: isinstance(tui.screen, JobScreen))
        assert load_jobs(jobs_file)[0].sheets == ['Overview', 'Sheet [old]']

    _drive(jobs_file, steps)


def test_failed_sheet_open_uses_the_typed_sheets_form(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = _notes(monkeypatch)
    login = 'Run: tabpull login --site finance'
    seen, _opened = _patch_view(monkeypatch, _view([], []), fail=login)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('enter', 's')
        await _until(pilot, lambda: isinstance(tui.screen, SheetsForm))
        assert isinstance(tui.screen, SheetsForm)
        assert tui.screen.query_one('#sheets', TextArea).text == 'Totals\nDetail'
        labels = [str(widget.render()) for widget in tui.screen.query('Label')]
        assert all('tabpull add' not in label for label in labels)
        assert notes == [login]
        assert seen == ['Sales/Overview']

        await pilot.press('escape', 's')
        await _until(pilot, lambda: isinstance(tui.screen, SheetsForm))
        assert seen == ['Sales/Overview']
        assert notes == [login, login]

    _drive(jobs_file, steps)


def _force_quit_child() -> None:
    root = Path(os.environ['TABPULL_QUIT_ROOT'])
    jobs = root / 'jobs.toml'
    save_jobs(jobs, [])
    tableau.save_site(
        'demo',
        {
            'TABLEAU_SERVER_URL': 'https://tableau.example',
            'TABLEAU_SITE': 'demo',
            'TABLEAU_PAT_NAME': 'tabpull',
            'TABLEAU_PAT_SECRET': 'pat-value',
        },
    )
    started = threading.Event()

    def hang(*_args: object, **_kwargs: object) -> list[object]:
        started.set()
        threading.Event().wait()
        return []

    tui_app.search_views = hang  # ty: ignore[invalid-assignment]
    app = TabpullApp(jobs, root / 'out')

    async def drive(pilot: Pilot[object]) -> None:
        await pilot.press('a')
        app.screen.query_one('#value', Input).value = 'overview'
        await pilot.press('ctrl+s')
        for _ in range(50):
            if started.is_set():
                break
            await pilot.pause(0.05)
        else:
            msg = 'search did not start'
            raise RuntimeError(msg)
        await pilot.press('escape')
        for _ in range(50):
            if type(app.screen).__name__ == 'HomeScreen':
                break
            await pilot.pause(0.05)
        else:
            msg = 'escape did not return home'
            raise RuntimeError(msg)
        await pilot.press('q')
        await pilot.press('q')

    app.run(headless=True, size=(100, 40), auto_pilot=drive)
    print('force-quit-returned', flush=True)


if __name__ == '__main__' and '--force-quit-child' in sys.argv:
    _force_quit_child()
