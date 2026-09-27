import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import DataTable, Input, RadioButton, Static, TextArea

from tabpull import tableau
from tabpull.jobs import Job, RangeFilter, ValuesFilter, load_jobs, save_jobs
from tabpull.tui.app import HomeScreen, JobScreen, TabpullApp
from tabpull.tui.forms import ConfirmScreen, FilterForm


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


def test_home_runs_marked_jobs_and_removes_after_confirm(
    jobs_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def shell(_self: TabpullApp, *argv: str) -> int:
        calls.append(argv)
        return 1

    monkeypatch.setattr(TabpullApp, 'shell', shell)

    async def steps(tui: TabpullApp, pilot: Pilot[None]) -> None:
        await pilot.press('r')
        assert calls == [('run', '--', 'daily')]
        assert 'FAILED' in tui.last_run

        await pilot.press('space', 'space', 'r')
        assert calls[-1] == ('run', '--', 'daily', 'weekly')

        await pilot.press('d')
        assert isinstance(tui.screen, ConfirmScreen)
        await pilot.press('n')
        assert len(load_jobs(jobs_file)) == 2

        await pilot.press('space', 'd', 'y')
        assert [job.name for job in load_jobs(jobs_file)] == ['weekly']
        assert tui.screen.query_one('#jobs', DataTable).row_count == 1

        await pilot.press('a')
        assert calls[-1] == ('add',)

    _drive(jobs_file, steps)
