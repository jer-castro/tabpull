import io
import runpy
import shlex
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self, cast

import pytest
from rich.console import Console

from tabpull import add, app, embed, run, tableau, ui, wizard
from tabpull.jobs import Job, JobError, RangeFilter, ValuesFilter, load_jobs, parse_job
from tabpull.tableau import Settings, UnknownSiteError


def _xdg(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(root / 'config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(root / 'data'))


def _site_values(
    server: str = 'https://tableau.example',
    site: str = 'finance',
    pat_name: str = 'tabpull',
    value: str = 'pat-value',
) -> dict[str, str]:
    return {
        'TABLEAU_SERVER_URL': server,
        'TABLEAU_SITE': site,
        'TABLEAU_PAT_NAME': pat_name,
        'TABLEAU_PAT_SECRET': value,
    }


def _settings(name: str, root: Path, value: str = 'pat-value') -> Settings:
    return Settings(
        'https://tableau.example',
        name,
        'tabpull',
        name,
        root / f'{name}.json',
        value,
    )


class _Playwright:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _notty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)


def _allow_view(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        def close(self) -> None:
            return None

    monkeypatch.setattr(add, 'sync_playwright', _Playwright)
    monkeypatch.setattr(add, 'browser_session', lambda *_args, **_kwargs: object())
    monkeypatch.setattr(add, 'open_view', lambda *_args, **_kwargs: Page())


def test_linux_paths_follow_xdg_and_ignore_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    _xdg(monkeypatch, tmp_path)

    assert tableau.config_dir() == tmp_path / 'config' / 'tabpull'
    assert tableau.data_dir() == tmp_path / 'data' / 'tabpull'
    assert tableau.jobs_path() == tmp_path / 'config' / 'tabpull' / 'jobs.toml'
    assert tableau.jobs_path() != work / 'jobs.toml'


def test_os_paths_when_xdg_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    monkeypatch.setattr(tableau.Path, 'home', lambda: tmp_path)

    monkeypatch.setattr(tableau.sys, 'platform', 'linux')
    assert tableau.config_dir() == tmp_path / '.config' / 'tabpull'
    assert tableau.data_dir() == tmp_path / '.local' / 'share' / 'tabpull'

    monkeypatch.setattr(tableau.sys, 'platform', 'darwin')
    assert (
        tableau.config_dir() == tmp_path / 'Library' / 'Application Support' / 'tabpull'
    )
    assert tableau.data_dir() == tmp_path / '.local' / 'share' / 'tabpull'

    monkeypatch.setattr(tableau.sys, 'platform', 'win32')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    assert tableau.config_dir() == tmp_path / 'Roaming' / 'tabpull'
    assert tableau.data_dir() == tmp_path / 'Local' / 'tabpull'

    monkeypatch.setattr(tableau.sys, 'platform', 'darwin')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg-config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg-data'))
    assert tableau.config_dir() == tmp_path / 'xdg-config' / 'tabpull'
    assert tableau.data_dir() == tmp_path / 'xdg-data' / 'tabpull'


def test_macos_auth_stays_and_old_exports_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    monkeypatch.setattr(tableau.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(tableau.sys, 'platform', 'darwin')
    config = tmp_path / 'Library' / 'Application Support' / 'tabpull'
    (config / 'exports' / 'daily').mkdir(parents=True)
    (config / 'exports' / 'daily' / 'sheet.csv').write_text('a\n', encoding='utf-8')
    (config / 'auth').mkdir()
    (config / 'auth' / 'finance.json').write_text('{}\n', encoding='utf-8')

    assert tableau.site_auth_path('finance') == config / 'auth' / 'finance.json'
    assert (config / 'auth' / 'finance.json').read_text(encoding='utf-8') == '{}\n'
    exported = config / 'exports' / 'daily' / 'sheet.csv'
    assert exported.read_text(encoding='utf-8') == 'a\n'
    assert not (tmp_path / '.local' / 'share' / 'tabpull' / 'exports').exists()


def test_linux_and_windows_auth_moves_into_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    monkeypatch.setattr(tableau.Path, 'home', lambda: tmp_path)

    monkeypatch.setattr(tableau.sys, 'platform', 'linux')
    data = tmp_path / '.local' / 'share' / 'tabpull'
    (data / 'auth').mkdir(parents=True)
    (data / 'auth' / 'finance.json').write_text('{}\n', encoding='utf-8')
    (data / 'exports').mkdir()
    (data / 'exports' / 'sheet.csv').write_text('a\n', encoding='utf-8')

    auth = tmp_path / '.config' / 'tabpull' / 'auth' / 'finance.json'
    assert tableau.site_auth_path('finance') == auth
    assert auth.read_text(encoding='utf-8') == '{}\n'
    assert not (data / 'auth').exists()
    assert (data / 'exports' / 'sheet.csv').read_text(encoding='utf-8') == 'a\n'

    monkeypatch.setattr(tableau.sys, 'platform', 'win32')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    local = tmp_path / 'Local' / 'tabpull'
    (local / 'auth').mkdir(parents=True)
    (local / 'auth' / 'finance.json').write_text('w\n', encoding='utf-8')
    (local / 'exports').mkdir()
    (local / 'exports' / 'sheet.csv').write_text('e\n', encoding='utf-8')

    windows_auth = tmp_path / 'Roaming' / 'tabpull' / 'auth' / 'finance.json'
    assert tableau.site_auth_path('finance') == windows_auth
    assert windows_auth.read_text(encoding='utf-8') == 'w\n'
    assert not (local / 'auth').exists()
    assert (local / 'exports' / 'sheet.csv').read_text(encoding='utf-8') == 'e\n'


def test_sites_keep_separate_tokens_and_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    (work / '.env').write_text(
        'TABLEAU_SERVER_URL=https://from-dotenv\nTABLEAU_PAT_SECRET=from-dotenv\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('TABLEAU_PAT_SECRET', 'from-env')
    _xdg(monkeypatch, tmp_path)
    alpha_value = 'alpha-token'
    beta_value = 'beta-token'
    tableau.save_site(
        'alpha', _site_values(server='https://a.example', value=alpha_value)
    )
    tableau.save_site(
        'beta', _site_values(server='https://b.example', site='', value=beta_value)
    )

    alpha = tableau.load_site('alpha')
    beta = tableau.load_site('beta')

    assert tableau.list_sites() == ['alpha', 'beta']
    assert alpha.server == 'https://a.example'
    assert alpha.pat_secret == alpha_value
    assert not beta.site
    assert beta.pat_secret == beta_value
    assert alpha.auth_path != beta.auth_path
    assert alpha.auth_path == tmp_path / 'config' / 'tabpull' / 'auth' / 'alpha.json'
    assert beta.auth_path.parent == alpha.auth_path.parent
    assert tableau.site_env_path('alpha').stat().st_mode & 0o777 == 0o600
    assert 'from-dotenv' not in tableau.site_env_path('alpha').read_text(
        encoding='utf-8'
    )
    assert 'alpha-token' not in repr(alpha)


def test_help_lists_the_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        app.main(['--help'])
    text = capsys.readouterr().out

    assert exc.value.code == 0
    for name in ('setup', 'add', 'run', 'login', '--jobs', '--out'):
        assert name in text


def test_add_help_lists_noninteractive_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        app.main(['add', '--help'])
    text = capsys.readouterr().out

    assert exc.value.code == 0
    for flag in ('--site', '--view', '--sheet', '--filter', '--param', '--name'):
        assert flag in text


def test_setup_subcommand_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str | None] = {}

    def fake(site_name: str | None = None) -> str:
        seen['site'] = site_name
        return site_name or 'created'

    with pytest.raises(SystemExit, match='Site name'):
        app.main(['setup', '--site', '../nope'])

    monkeypatch.setattr(wizard, 'main', fake)

    assert app.main(['setup', '--site', 'finance']) == 0
    assert seen['site'] == 'finance'


def test_add_flags_write_a_job_without_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    _xdg(monkeypatch, tmp_path)
    tableau.save_site('demo', _site_values())
    tableau.save_site('other', _site_values(server='https://other.example'))
    jobs = tmp_path / 'scratch.toml'

    def refuse_prompt(prompt: str = '') -> str:
        msg = f'prompted: {prompt}'
        raise AssertionError(msg)

    monkeypatch.setattr('builtins.input', refuse_prompt)
    _allow_view(monkeypatch)
    code = app.main(
        [
            '--jobs',
            str(jobs),
            'add',
            '--site',
            'demo',
            '--view',
            'https://tableau.example/#/site/finance/views/Sales/Overview',
            '--sheet',
            'Order Detail',
            '--sheet',
            'Totals',
            '--filter',
            'Region=West|Central',
            '--filter',
            'Order Date=2026-09-01..2026-09-25 @Totals',
            '--param',
            'Top N=25',
            '--name',
            'daily-west',
        ]
    )
    job = parse_job(tomllib.loads(jobs.read_text(encoding='utf-8'))['job'][0])
    out = capsys.readouterr().out

    assert code == 0
    assert job.name == 'daily-west'
    assert job.site == 'demo'
    assert job.view == 'Sales/Overview'
    assert job.sheets == ['Order Detail', 'Totals']
    assert job.params == {'Top N': '25'}
    assert job.filters[0] == ValuesFilter('Region', ['West', 'Central'], 'Order Detail')
    assert job.filters[1] == RangeFilter(
        'Order Date', 'Totals', '2026-09-01', '2026-09-25'
    )
    assert "filter 'Region' names no sheet" in out
    assert 'Order Detail' in out
    assert 'pat-value' not in out
    assert 'https://tableau.example' in out
    assert not tableau.jobs_path().exists()
    assert not (work / 'jobs.toml').exists()
    assert not (work / 'exports').exists()
    with pytest.raises(SystemExit, match='already exists'):
        app.main(
            [
                '--jobs',
                str(jobs),
                'add',
                '--site',
                'demo',
                '--view',
                'Sales/Overview',
                '--sheet',
                'Order Detail',
                '--name',
                'daily-west',
            ]
        )


def test_add_flags_imply_the_only_site_and_default_jobs_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _xdg(monkeypatch, tmp_path)
    tableau.save_site('demo', _site_values())
    monkeypatch.setattr(
        'builtins.input',
        lambda prompt='': (_ for _ in ()).throw(AssertionError(prompt)),
    )
    _allow_view(monkeypatch)

    assert (
        app.main(
            ['add', '--view', 'Sales/Overview', '--sheet', 'Detail', '--name', 'j']
        )
        == 0
    )
    jobs = load_jobs(tableau.jobs_path())

    assert [job.name for job in jobs] == ['j']
    assert jobs[0].site == 'demo'
    assert not (tmp_path / 'jobs.toml').exists()


def _scripted_ask(monkeypatch: pytest.MonkeyPatch, answers: list[object]) -> None:
    queued = iter(answers)

    def ask(question: object) -> object:
        try:
            return next(queued)
        except StopIteration as e:
            raise AssertionError(question) from e

    monkeypatch.setattr(ui, 'ask', ask)
    monkeypatch.setattr(ui, 'interactive', lambda: True)


def test_prompted_add_keeps_the_sheet_on_each_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = tmp_path / 'jobs.toml'
    region = {
        'field': 'Region',
        'sheet': 'A Title Sheet',
        'type': 'categorical',
        'current': 'West',
    }
    order_date = {
        'field': 'Order Date',
        'sheet': 'A Title Sheet',
        'type': 'range',
        'current': '1/3/2023 .. 12/30/2026',
    }
    ship = {
        'field': 'Ship Mode',
        'sheet': 'B Real Sheet',
        'type': 'categorical',
        'current': '(All)',
    }
    _scripted_ask(
        monkeypatch,
        [
            ['B Real Sheet', 'A Title Sheet'],
            region,
            'West',
            order_date,
            '1/3/2024',
            '2/1/2024',
            ship,
            'First Class',
            'Done',
        ],
    )

    class Page:
        def evaluate(self, script: str, arg: object = None) -> dict[str, object]:
            assert script == embed.INSPECT_JS
            return {
                'sheets': [
                    {
                        'name': 'A Title Sheet',
                        'filters': [
                            {
                                'field': 'Region',
                                'type': 'categorical',
                                'current': 'West',
                            },
                            {
                                'field': 'Order Date',
                                'type': 'range',
                                'current': '1/3/2023 .. 12/30/2026',
                            },
                        ],
                    },
                    {
                        'name': 'B Real Sheet',
                        'filters': [
                            {
                                'field': 'Ship Mode',
                                'type': 'categorical',
                                'current': '(All)',
                            },
                        ],
                    },
                ],
                'params': [],
            }

        def close(self) -> None:
            return None

    class Item:
        name = 'Dashboard 1'
        content_url = 'CrosstabMe/sheets/Dashboard1'

    monkeypatch.setattr(add, 'sync_playwright', _Playwright)
    monkeypatch.setattr(add, 'browser_session', lambda *_a, **_k: object())
    monkeypatch.setattr(add, 'open_view', lambda *_a, **_k: Page())
    monkeypatch.setattr(add, '_find_view', lambda _settings: Item())

    add.add_job(_settings('demo', tmp_path), jobs, name='prompted')
    job = load_jobs(jobs)[0]

    assert 'names no sheet' not in capsys.readouterr().out
    assert job.sheets == ['B Real Sheet', 'A Title Sheet']
    assert job.filters == [
        ValuesFilter('Region', ['West'], 'A Title Sheet'),
        RangeFilter('Order Date', 'A Title Sheet', '1/3/2024', '2/1/2024'),
        ValuesFilter('Ship Mode', ['First Class'], 'B Real Sheet'),
    ]


def test_prompted_add_asks_again_when_both_range_bounds_are_blank(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = tmp_path / 'jobs.toml'
    order_date = {
        'field': 'Order Date',
        'sheet': 'A Title Sheet',
        'type': 'range',
        'current': '',
    }
    _scripted_ask(
        monkeypatch,
        [['A Title Sheet'], order_date, '', ' ', '', '2/1/2024', 'Done'],
    )

    class Page:
        def evaluate(self, script: str, arg: object = None) -> dict[str, object]:
            return {
                'sheets': [
                    {
                        'name': 'A Title Sheet',
                        'filters': [
                            {'field': 'Order Date', 'type': 'range', 'current': ''},
                        ],
                    }
                ],
                'params': [],
            }

        def close(self) -> None:
            return None

    class Item:
        name = 'Dashboard 1'
        content_url = 'CrosstabMe/sheets/Dashboard1'

    monkeypatch.setattr(add, 'sync_playwright', _Playwright)
    monkeypatch.setattr(add, 'browser_session', lambda *_a, **_k: object())
    monkeypatch.setattr(add, 'open_view', lambda *_a, **_k: Page())
    monkeypatch.setattr(add, '_find_view', lambda _settings: Item())

    add.add_job(_settings('demo', tmp_path), jobs, name='prompted')

    assert 'Give a from, a to, or both.' in capsys.readouterr().out
    assert load_jobs(jobs)[0].filters == [
        RangeFilter('Order Date', 'A Title Sheet', None, '2/1/2024'),
    ]


def test_prompted_add_refuses_a_relative_range_before_saving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    message = add._validate_range_answer('yesterday')
    assert message == (
        "'yesterday' is a relative date or a date computed at run time; "
        'use YYYY-MM-DD or M/D/YYYY'
    )
    assert add._validate_range_answer('1/3/2024') is True
    jobs = tmp_path / 'jobs.toml'
    _scripted_ask(
        monkeypatch,
        [
            ['A Title Sheet'],
            {
                'field': 'Order Date',
                'sheet': 'A Title Sheet',
                'type': 'range',
                'current': '',
            },
            'yesterday',
        ],
    )

    class Page:
        def evaluate(self, script: str, arg: object = None) -> dict[str, object]:
            return {
                'sheets': [
                    {
                        'name': 'A Title Sheet',
                        'filters': [
                            {
                                'field': 'Order Date',
                                'type': 'range',
                                'current': '',
                            },
                        ],
                    }
                ],
                'params': [],
            }

        def close(self) -> None:
            return None

    class Item:
        name = 'Dashboard 1'
        content_url = 'CrosstabMe/sheets/Dashboard1'

    monkeypatch.setattr(add, 'sync_playwright', _Playwright)
    monkeypatch.setattr(add, 'browser_session', lambda *_a, **_k: object())
    monkeypatch.setattr(add, 'open_view', lambda *_a, **_k: Page())
    monkeypatch.setattr(add, '_find_view', lambda _settings: Item())

    with pytest.raises(JobError, match='relative date'):
        add.add_job(_settings('demo', tmp_path), jobs, name='prompted')
    assert not jobs.exists()


def test_add_without_flags_still_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _xdg(monkeypatch, tmp_path)
    tableau.save_site('demo', _site_values())
    seen: list[str] = []
    monkeypatch.setattr(
        app,
        'add_job',
        lambda settings, path, name=None: seen.append(settings.name),
    )
    monkeypatch.setattr(
        app,
        'add_job_from_flags',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('flags')),
    )

    monkeypatch.setattr(ui, 'interactive', lambda: True)
    assert app.main(['add']) == 0
    assert seen == ['demo']
    with pytest.raises(SystemExit, match='--view'):
        app.main(['add', '--sheet', 'A'])
    with pytest.raises(SystemExit, match='--sheet'):
        app.main(['add', '--view', 'Sales/Overview', '--filter', 'Region=West'])


def test_prompted_add_without_a_tty_exits_with_the_view_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _xdg(monkeypatch, tmp_path)
    tableau.save_site('demo', _site_values())
    monkeypatch.setattr(ui, 'interactive', lambda: False)

    def refuse(question: object) -> object:
        raise AssertionError(question)

    monkeypatch.setattr(ui, 'ask', refuse)
    monkeypatch.setattr(
        app,
        'add_job',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('add')),
    )
    with pytest.raises(SystemExit, match='Pass --view'):
        app.main(['add'])


def test_pick_site_asks_when_several_sites_are_interactive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _xdg(monkeypatch, tmp_path)
    tableau.save_site('alpha', _site_values())
    tableau.save_site('beta', _site_values(server='https://b.example', site='ops'))
    monkeypatch.setattr(ui, 'interactive', lambda: True)
    seen: list[object] = []

    def ask(question: object) -> str:
        seen.append(question)
        return 'beta'

    monkeypatch.setattr(ui, 'ask', ask)
    settings = app._pick_site(None)

    assert settings.name == 'beta'
    assert settings.server == 'https://b.example'
    assert settings.site == 'ops'
    assert len(seen) == 1


def test_flag_add_and_run_refuse_a_story(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _xdg(monkeypatch, tmp_path)
    tableau.save_site('demo', _site_values())
    monkeypatch.setattr(add, 'sync_playwright', _Playwright)
    monkeypatch.setattr(add, 'browser_session', lambda *_args, **_kwargs: object())

    def refuse_story(_context: object, _settings: Settings, view: str) -> object:
        msg = 'Stories are not supported; use the dashboard inside it.'
        raise JobError(msg)

    monkeypatch.setattr(add, 'open_view', refuse_story)
    monkeypatch.setattr(
        'builtins.input',
        lambda prompt='': (_ for _ in ()).throw(AssertionError(prompt)),
    )
    with pytest.raises(SystemExit, match='Stories are not supported'):
        app.main(
            [
                'add',
                '--view',
                'Book/Story1',
                '--sheet',
                'A Title',
                '--name',
                'story-job',
            ]
        )
    assert not tableau.jobs_path().exists()

    jobs = tmp_path / 'jobs.toml'
    out = tmp_path / 'out'
    jobs.write_text(
        """
[[job]]
name = "story-job"
site = "demo"
view = "Book/Story1"
sheets = ["A Title"]

[[job]]
name = "ok"
site = "demo"
view = "Book/Dash"
sheets = ["A"]
""",
        encoding='utf-8',
    )
    # The download path is unused: a story raises before export, and the dashboard
    # job's fake page has no file. Point expect_download at a real CSV.
    raw = tmp_path / 'download.csv'
    raw.write_text('a,b\n1,2\n', encoding='utf-8')

    class Download:
        def path(self) -> str:
            return str(raw)

    class Expect:
        value = Download()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def open_view_for_run(_context: object, _settings: Settings, view: str) -> object:
        if view == 'Book/Story1':
            msg = 'Stories are not supported; use the dashboard inside it.'
            raise JobError(msg)

        class Page:
            def evaluate(self, script: str, arg: object = None) -> None:
                return None

            def expect_download(self, timeout: int) -> Expect:
                return Expect()

            def close(self) -> None:
                return None

        return Page()

    monkeypatch.setattr(run, 'sync_playwright', _Playwright)
    monkeypatch.setattr(run, 'browser_session', lambda *_args, **_kwargs: object())
    monkeypatch.setattr(embed, 'open_view', open_view_for_run)
    code = app.main(['--jobs', str(jobs), '--out', str(out), 'run'])
    text = capsys.readouterr().out

    assert code == 1
    assert (
        '✗ story-job: Stories are not supported; use the dashboard inside it.' in text
    )
    assert '✓ ok:' in text
    assert not (out / 'story-job').exists()
    assert (out / 'ok' / 'A.csv').is_file()


def test_login_and_run_name_each_site_without_printing_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _xdg(monkeypatch, tmp_path)
    hidden = 'do-not-print'
    tableau.save_site('demo', _site_values(value=hidden))
    monkeypatch.setattr(app, 'sync_playwright', _Playwright)
    monkeypatch.setattr(app, 'sso_login', lambda _pw, _settings: None)

    assert app.main(['login']) == 0
    login_out = capsys.readouterr().out
    assert 'https://tableau.example' in login_out
    assert 'local name demo' in login_out
    assert hidden not in login_out

    jobs = tmp_path / 'jobs.toml'
    out = tmp_path / 'out'
    jobs.write_text(
        """
[[job]]
name = "a"
site = "one"
view = "W/V"
sheets = ["S"]

[[job]]
name = "b"
site = "one"
view = "W/V"
sheets = ["S"]

[[job]]
name = "c"
site = "two"
view = "W/V"
sheets = ["S"]

[[job]]
name = "bad"
site = "two"
view = "W/V"
sheets = ["S"]

[[job]]
name = "gone-job"
site = "gone"
view = "W/V"
sheets = ["S"]
""",
        encoding='utf-8',
    )
    opened: list[str] = []

    def load_site(name: str) -> Settings:
        if name == 'gone':
            msg = "No site named 'gone'."
            raise UnknownSiteError(msg)
        return _settings(name, tmp_path, value=hidden)

    def export_embed(
        _context: object, settings: Settings, job: Job, out_dir: Path, **_: object
    ) -> list[Path]:
        if job.name == 'bad':
            msg = 'broken view'
            raise JobError(msg)
        return [out_dir / f'{settings.name}-{job.name}.csv']

    monkeypatch.setattr(run, 'sync_playwright', _Playwright)
    monkeypatch.setattr(run, 'load_site', load_site)
    monkeypatch.setattr(
        run, 'browser_session', lambda _pw, settings: opened.append(settings.name)
    )
    monkeypatch.setattr(run, 'export_embed', export_embed)

    code = app.main(['--jobs', str(jobs), 'run', '--out', str(out)])
    run_out = capsys.readouterr().out

    assert code == 1
    assert opened == ['one', 'two']
    assert '✓ a:' in run_out
    assert '✓ b:' in run_out
    assert '✓ c:' in run_out
    assert '✗ bad: broken view' in run_out
    assert '✗ gone-job: No site named' in run_out
    assert hidden not in run_out
    assert f'Exporting 5 job(s) to {out}/' in run_out
    assert 'done: 3/5 jobs exported' in run_out
    assert f'tabpull run --jobs {jobs} --out {out} -- bad gone-job' in run_out


def test_run_exports_into_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _xdg(monkeypatch, tmp_path)
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    jobs = tmp_path / 'jobs.toml'
    jobs.write_text(
        """
[[job]]
name = "a"
site = "one"
view = "W/V"
sheets = ["S"]
""",
        encoding='utf-8',
    )
    seen: list[Path] = []
    monkeypatch.setattr(run, 'sync_playwright', _Playwright)
    monkeypatch.setattr(run, 'load_site', lambda name: _settings(name, tmp_path))
    monkeypatch.setattr(run, 'browser_session', lambda _pw, _settings: object())
    monkeypatch.setattr(
        run,
        'export_embed',
        lambda _context, _settings, _job, out_dir, **_: (
            seen.append(out_dir) or [out_dir / 'a.csv']
        ),
    )

    assert app.main(['--jobs', str(jobs), 'run', 'a']) == 0
    assert [path.resolve() for path in seen] == [work.resolve()]


def test_run_help_documents_filter_overrides(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        app.main(['run', '--help'])
    text = capsys.readouterr().out

    assert exc.value.code == 0
    assert '--filter' in text
    assert 'every job' in text
    assert 'does not rewrite' in text
    assert 'Order Date=2026-09-01..' in text
    assert 'daily-west weekly-east' in text


def test_run_filter_overrides_every_selected_job_without_rewriting_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = tmp_path / 'jobs.toml'
    out = tmp_path / 'out'
    jobs.write_text(
        """
[[job]]
name = "daily"
site = "one"
view = "W/V"
sheets = ["Totals", "Detail"]
filters = [
  { field = "Order Date", min = "2026-09-01", max = "2026-09-25", sheet = "Totals" },
  { field = "Region", values = ["West"], sheet = "Detail" },
]

[[job]]
name = "weekly"
site = "one"
view = "W/V"
sheets = ["Detail"]
""",
        encoding='utf-8',
    )
    saved = jobs.read_text(encoding='utf-8')
    seen: list[Job] = []

    def export_embed(
        _context: object, _settings: object, job: Job, out_dir: Path, **_kwargs: object
    ) -> list[Path]:
        seen.append(job)
        return [out_dir / f'{job.name}.csv']

    monkeypatch.setattr(run, 'sync_playwright', _Playwright)
    monkeypatch.setattr(run, 'load_site', lambda name: _settings(name, tmp_path))
    monkeypatch.setattr(run, 'browser_session', lambda *_a, **_k: object())
    monkeypatch.setattr(run, 'export_embed', export_embed)

    code = app.main(
        [
            '--jobs',
            str(jobs),
            '--out',
            str(out),
            'run',
            '--filter',
            'Order Date=2026-10-01..',
            '--filter',
            'Ship Mode=First Class @Detail',
        ]
    )
    note = capsys.readouterr().out

    assert code == 0
    assert jobs.read_text(encoding='utf-8') == saved
    assert [job.name for job in seen] == ['daily', 'weekly']
    assert seen[0].filters == [
        RangeFilter('Order Date', 'Totals', '2026-10-01', None),
        ValuesFilter('Region', ['West'], 'Detail'),
        ValuesFilter('Ship Mode', ['First Class'], 'Detail'),
    ]
    assert seen[1].filters == [
        RangeFilter('Order Date', 'Detail', '2026-10-01', None),
        ValuesFilter('Ship Mode', ['First Class'], 'Detail'),
    ]
    assert 'daily: filter' not in note
    assert "weekly: filter 'Order Date' names no sheet" in note
    assert "applying it on 'Detail'" in note
    assert 'Ship Mode' not in note


def test_run_filter_refuses_a_relative_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jobs = tmp_path / 'jobs.toml'
    jobs.write_text(
        """
[[job]]
name = "daily"
site = "one"
view = "W/V"
sheets = ["Totals"]
filters = [
  { field = "Order Date", min = "2026-09-01", max = "2026-09-25", sheet = "Totals" },
]
""",
        encoding='utf-8',
    )
    saved = jobs.read_text(encoding='utf-8')

    def export_embed(*_args: object, **_kwargs: object) -> list[Path]:
        msg = 'exported'
        raise AssertionError(msg)

    monkeypatch.setattr(run, 'sync_playwright', _Playwright)
    monkeypatch.setattr(run, 'export_embed', export_embed)

    with pytest.raises(SystemExit, match='relative date') as exc:
        app.main(
            [
                '--jobs',
                str(jobs),
                'run',
                'daily',
                '--filter',
                'Order Date=yesterday..',
            ]
        )

    assert 'YYYY-MM-DD or M/D/YYYY' in str(exc.value)
    assert jobs.read_text(encoding='utf-8') == saved


def test_failed_run_rerun_repeats_the_filter_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = tmp_path / 'jobs.toml'
    jobs.write_text(
        """
[[job]]
name = "daily"
site = "one"
view = "W/V"
sheets = ["Totals"]
""",
        encoding='utf-8',
    )
    saved = jobs.read_text(encoding='utf-8')
    fail = [True]

    def export_embed(
        _context: object, _settings: object, job: Job, out_dir: Path, **_kwargs: object
    ) -> list[Path]:
        if fail.pop():
            msg = 'boom'
            raise JobError(msg)
        return [out_dir / f'{job.name}.csv']

    monkeypatch.setattr(run, 'sync_playwright', _Playwright)
    monkeypatch.setattr(run, 'load_site', lambda name: _settings(name, tmp_path))
    monkeypatch.setattr(run, 'browser_session', lambda *_a, **_k: object())
    monkeypatch.setattr(run, 'export_embed', export_embed)

    assert (
        app.main(
            [
                '--jobs',
                str(jobs),
                '--out',
                'reports',
                'run',
                '--filter',
                'Order Date=2026-09-01..',
            ]
        )
        == 1
    )
    help_line = capsys.readouterr().out.splitlines()[-1]
    rerun = help_line.split('rerun them: ', 1)[1]

    assert 'Order Date=2026-09-01..' in rerun
    fail.append(False)
    assert app.main(shlex.split(rerun)[1:]) == 0
    assert jobs.read_text(encoding='utf-8') == saved


def test_failed_run_prints_a_rerun_command_that_selects_the_failed_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _xdg(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    jobs = tmp_path / 'jobs.toml'
    jobs.write_text(
        """
[[job]]
name = "-daily"
site = "one"
view = "W/V"
sheets = ["S"]
""",
        encoding='utf-8',
    )
    fail = [True]

    def export(
        _context: object, _settings: object, job: Job, out_dir: Path, **_: object
    ) -> list[Path]:
        if fail.pop():
            msg = 'boom'
            raise JobError(msg)
        return [out_dir / f'{job.name}.csv']

    monkeypatch.setattr(run, 'sync_playwright', _Playwright)
    monkeypatch.setattr(run, 'load_site', lambda name: _settings(name, tmp_path))
    monkeypatch.setattr(run, 'browser_session', lambda _pw, _settings: object())
    monkeypatch.setattr(run, 'export_embed', export)

    assert app.main(['--jobs', str(jobs), '--out', 'reports', 'run']) == 1
    help_line = capsys.readouterr().out.splitlines()[-1]
    rerun = help_line.split('rerun them: ', 1)[1]

    fail.append(False)
    assert app.main(shlex.split(rerun)[1:]) == 0
    assert 'done: 1/1 jobs exported' in capsys.readouterr().out


def test_tty_run_summary_lists_the_failed_job_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jobs = tmp_path / 'jobs.toml'
    out = tmp_path / 'out'
    jobs.write_text(
        """
[[job]]
name = "good-job"
site = "one"
view = "W/V"
sheets = ["Alpha"]

[[job]]
name = "bad-job"
site = "one"
view = "W/V"
sheets = ["Gamma"]
""",
        encoding='utf-8',
    )

    def export_embed(
        _context: object,
        _settings: object,
        job: Job,
        out_dir: Path,
        *,
        on_sheet: Callable[[int, str], None],
    ) -> list[Path]:
        on_sheet(0, job.sheets[0])
        if job.name == 'bad-job':
            msg = 'broken view'
            raise JobError(msg)
        return [out_dir / f'{job.name}.csv']

    buffer = io.StringIO()
    monkeypatch.setattr(run, 'sync_playwright', _Playwright)
    monkeypatch.setattr(run, 'load_site', lambda name: _settings(name, tmp_path))
    monkeypatch.setattr(run, 'browser_session', lambda *_a, **_k: object())
    monkeypatch.setattr(run, 'export_embed', export_embed)
    monkeypatch.setattr(ui, 'rich_output', lambda: True)
    monkeypatch.setattr(ui, 'console', Console(file=buffer, width=120))

    code = app.main(['--jobs', str(jobs), '--out', str(out), 'run'])

    assert code == 1
    assert 'bad-job' in buffer.getvalue()
    assert 'good-job' in buffer.getvalue()
    assert 'broken view' in buffer.getvalue()


def test_home_lists_sites_and_jobs_or_says_there_are_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _xdg(monkeypatch, tmp_path)
    jobs = tmp_path / 'jobs.toml'

    assert app.main(['--jobs', str(jobs)]) == 0
    empty = capsys.readouterr().out
    assert 'sites: 0 configured' in empty
    assert 'jobs: 0 saved' in empty
    assert 'tabpull setup' in empty

    tableau.save_site('finance', _site_values())
    jobs.write_text(
        """
[[job]]
name = "-daily"
site = "finance"
view = "Sales, Inc/Overview"
sheets = ["A", "B"]
""",
        encoding='utf-8',
    )
    assert app.main(['--jobs', str(jobs)]) == 0
    text = capsys.readouterr().out
    assert (
        'sites[1]{name,server,site}:\n  finance,"https://tableau.example",finance'
        in text
    )
    assert (
        'jobs[1]{name,site,view,sheets}:\n  "-daily",finance,"Sales, Inc/Overview",2'
        in text
    )
    assert f'tabpull run <name> --jobs {jobs}' in text

    assert app.main(['--jobs', str(jobs), '--out', 'reports']) == 0
    add_line = next(
        line for line in capsys.readouterr().out.splitlines() if 'tabpull add' in line
    )
    assert '--out' not in add_line
    add_cmd = add_line.split('`')[1].replace('<Workbook/View>', 'W/V')
    args, extra = app._parser()[0].parse_known_args(shlex.split(add_cmd)[1:])
    assert (args.command, str(args.jobs), extra) == ('add', str(jobs), [])


def test_unknown_subcommand_flag_prints_that_commands_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        app.main(['run', '--stat', 'x'])
    text = capsys.readouterr().out

    assert exc.value.code == 2
    assert 'error: unrecognized arguments: --stat' in text
    assert 'usage: tabpull run' in text
    assert '--out' in text


def test_several_sites_require_a_flag_when_there_is_no_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _xdg(monkeypatch, tmp_path)
    _notty(monkeypatch)
    tableau.save_site('alpha', _site_values())
    tableau.save_site('beta', _site_values(server='https://b.example'))

    with pytest.raises(SystemExit, match='--site'):
        app.main(['login'])
    with pytest.raises(SystemExit, match='--site'):
        app.main(['add', '--view', 'Sales/Overview', '--sheet', 'Detail'])


def test_missing_session_names_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _notty(monkeypatch)
    monkeypatch.setattr(tableau, 'launch_browser', lambda _pw, *, headless: object())
    settings = _settings('demo', tmp_path)

    with pytest.raises(SystemExit, match=r'tabpull login --site demo'):
        tableau.browser_session(cast('Any', object()), settings)


def test_setup_writes_each_site_under_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    (work / '.env').write_text('TABLEAU_PAT_SECRET=from-dotenv\n', encoding='utf-8')
    _xdg(monkeypatch, tmp_path)
    first_value = 'super-secret-value'
    third_value = 'other-secret'
    _scripted_ask(
        monkeypatch,
        [
            None,
            'https://tableau.example.com/#/site/finance/home',
            True,
            'tabpull',
            first_value,
            False,
            None,
            '',
            'tabpull',
            '',
            False,
            None,
            'https://other.example/t/ops/views/Book/Dash',
            True,
            'other-token',
            third_value,
            False,
        ],
    )
    monkeypatch.setattr(wizard, 'open_url', lambda _url: None)
    monkeypatch.setattr(wizard, 'rest_session', lambda _settings: _Playwright())
    monkeypatch.setattr(
        wizard,
        'sync_playwright',
        lambda: (_ for _ in ()).throw(AssertionError('browser')),
    )

    assert wizard.main('finance') == 'finance'
    assert wizard.main('finance') == 'finance'
    assert wizard.main('ops') == 'ops'
    text = capsys.readouterr().out
    finance = tableau.load_site('finance')
    ops = tableau.load_site('ops')

    assert finance.server == 'https://tableau.example.com'
    assert finance.site == 'finance'
    assert finance.pat_secret == first_value
    assert ops.server == 'https://other.example'
    assert ops.site == 'ops'
    assert ops.view_url('Book/Dash') == 'https://other.example/t/ops/views/Book/Dash'
    assert ops.pat_secret == third_value
    assert finance.auth_path != ops.auth_path
    assert not finance.auth_path.exists()
    assert first_value not in text
    assert third_value not in text
    assert 'tabpull add --site finance' in text
    assert str(tableau.jobs_path()) in text
    assert 'folder you run tabpull from' in text
    assert first_value not in (work / '.env').read_text(encoding='utf-8')


def test_source_files_point_at_the_command(capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ('app.py', 'wizard.py'):
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(root / 'src' / 'tabpull' / name), run_name='__main__')
        assert exc.value.code == 2
        assert 'tabpull' in capsys.readouterr().err
