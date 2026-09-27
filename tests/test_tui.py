import asyncio
import threading
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from playwright.sync_api import Playwright

import pytest
from textual.pilot import Pilot
from textual.widgets import Checkbox, DataTable, Input, RadioButton, Static, TextArea

from tabpull import tableau, ui
from tabpull.jobs import Job, RangeFilter, ValuesFilter, load_jobs, save_jobs
from tabpull.run import Report
from tabpull.tui.app import HomeScreen, JobScreen, TabpullApp
from tabpull.tui.forms import (
    ChecksForm,
    ChoiceScreen,
    ConfirmScreen,
    FilterForm,
    PickScreen,
    SetupForm,
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


def test_home_lists_jobs_and_edits_a_filter_param_and_name(jobs_file: Path) -> None:
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
        table = tui.screen.query_one('#jobs', DataTable)
        assert table.get_row_at(0)[1] == 'daily-west'

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


def test_setup_save_anyway_and_a_refused_check_stays_on_the_form(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_settings: object) -> None:
        msg = 'token rejected'
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
