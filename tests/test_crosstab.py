import tomllib
from pathlib import Path
from typing import Any, Self, cast

import pytest
from playwright.sync_api import Error as PlaywrightError

from tabpull import crosstab
from tabpull.crosstab import (
    Job,
    JobError,
    RangeFilter,
    ValuesFilter,
    job_to_toml,
    match_score,
    normalize_csv,
    parse_filter_spec,
    parse_job,
    resolved_filters,
    view_from_flag,
)
from tabpull.tableau import Settings, parse_tableau_url


def test_normalize_csv_rewrites_tableau_utf16_tsv_as_utf8_csv() -> None:
    raw = 'Region\tSales, total\nWest\t"1,200"\nSão Paulo\t5\n'.encode('utf-16')

    assert normalize_csv(raw) == 'Region,"Sales, total"\nWest,"1,200"\nSão Paulo,5\n'


def test_normalize_csv_passes_utf8_through_without_bom() -> None:
    assert normalize_csv('\ufeffa,b\n1,2\n'.encode()) == 'a,b\n1,2\n'


@pytest.mark.parametrize(
    ('url', 'server', 'site', 'view'),
    [
        (
            'https://prod-useast-a.online.tableau.com/#/site/demo/views/Sales/Overview?:iid=1',
            'https://prod-useast-a.online.tableau.com',
            'demo',
            'Sales/Overview',
        ),
        (
            'https://tableau.corp.com/t/finance/views/Budget/Dash',
            'https://tableau.corp.com',
            'finance',
            'Budget/Dash',
        ),
        (
            'https://tableau.corp.com/#/views/Budget/Dash',
            'https://tableau.corp.com',
            '',
            'Budget/Dash',
        ),
        (
            'https://tableau.corp.com/#/site/finance/home',
            'https://tableau.corp.com',
            'finance',
            None,
        ),
    ],
)
def test_parse_tableau_url(url: str, server: str, site: str, view: str | None) -> None:
    parsed = parse_tableau_url(url)

    assert (parsed.server, parsed.site, parsed.view) == (server, site, view)


def test_jobs_round_trip_through_toml() -> None:
    job = Job(
        'west "q1"',
        'Sales/Overview',
        ['Detail Table', 'Totals'],
        'demo',
        [
            ValuesFilter('Region', ['West', 'East'], 'Totals'),
            RangeFilter('Order Date', 'Detail Table', '2026-01-01', None),
        ],
        {'Top N': '10'},
    )

    assert [parse_job(raw) for raw in tomllib.loads(job_to_toml(job))['job']] == [job]


@pytest.mark.parametrize(
    ('query', 'matches'),
    [
        ('sales overview', True),
        ('Dashbord', True),
        ('overview sales', True),
        ('inventory', False),
        ('sales inventory', False),
    ],
)
def test_match_score_finds_views_by_words_and_typos(query: str, matches: bool) -> None:
    text = 'Sales Dashboard SalesWorkbook/sheets/Overview'

    assert (match_score(query, text) >= crosstab.MIN_MATCH_SCORE) is matches


def test_filter_without_a_sheet_stays_blank_until_resolved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = parse_job(
        {
            'name': 'j',
            'site': 'demo',
            'view': 'W/V',
            'sheets': ['A', 'B'],
            'filters': [
                {'field': 'Region', 'values': ['West']},
                {'field': 'Year', 'values': ['2024'], 'sheet': 'B'},
            ],
        }
    )

    assert job.filters == [
        ValuesFilter('Region', ['West'], ''),
        ValuesFilter('Year', ['2024'], 'B'),
    ]
    resolved = resolved_filters(job)
    note = capsys.readouterr().out

    assert resolved == [
        ValuesFilter('Region', ['West'], 'A'),
        ValuesFilter('Year', ['2024'], 'B'),
    ]
    assert "filter 'Region' names no sheet" in note
    assert "'A'" in note
    assert 'Year' not in note
    assert 'B' not in note
    assert resolved_filters(Job('j', 'W/V', ['A', 'B'], 'demo', resolved)) == resolved
    assert not capsys.readouterr().out


def test_parse_job_requires_a_site() -> None:
    with pytest.raises(JobError, match='needs a site'):
        parse_job({'name': 'j', 'view': 'W/V', 'sheets': ['A']})


def test_blank_filter_sheet_round_trips_as_blank() -> None:
    job = Job('j', 'W/V', ['A', 'B'], 'demo', [ValuesFilter('Region', ['West'])])

    parsed = parse_job(tomllib.loads(job_to_toml(job))['job'][0])

    assert parsed.filters == [ValuesFilter('Region', ['West'], '')]


@pytest.mark.parametrize(
    ('spec', 'expected'),
    [
        ('Region=West|Central', ValuesFilter('Region', ['West', 'Central'], '')),
        (
            'Region=West|Central @Order Detail',
            ValuesFilter('Region', ['West', 'Central'], 'Order Detail'),
        ),
        (
            'Order Date=2026-09-01..2026-09-25 @Totals',
            RangeFilter('Order Date', 'Totals', '2026-09-01', '2026-09-25'),
        ),
        ('Order Date=2026-09-01..', RangeFilter('Order Date', '', '2026-09-01', None)),
    ],
)
def test_parse_filter_spec(spec: str, expected: ValuesFilter | RangeFilter) -> None:
    assert parse_filter_spec(spec) == expected


def test_parse_filter_spec_rejects_a_bare_word() -> None:
    with pytest.raises(JobError, match='Field='):
        parse_filter_spec('Region')


def test_view_from_flag_accepts_a_url_or_a_path() -> None:
    url = 'https://online.tableau.com/#/site/demo/views/Sales/Overview?:iid=1'

    assert view_from_flag('Sales/Overview') == 'Sales/Overview'
    assert view_from_flag(url) == 'Sales/Overview'
    with pytest.raises(JobError, match='no view'):
        view_from_flag('https://online.tableau.com/#/site/demo/home')
    with pytest.raises(JobError, match='Workbook/View'):
        view_from_flag('Overview')


class _LoadedView:
    def __init__(self, story: bool, closed: list[bool]) -> None:
        self.story = story
        self._closed = closed

    def route(self, *_args: object, **_kwargs: object) -> None:
        return None

    def goto(self, _url: str) -> None:
        return None

    def wait_for_function(self, *_args: object, **_kwargs: object) -> None:
        return None

    def evaluate(self, script: str, arg: object = None) -> str | None:
        if script == '() => window.vizState':
            return 'ready'
        if script != crosstab.STORY_JS:
            msg = f'unexpected {script!r}'
            raise AssertionError(msg)
        if self.story:
            msg = (
                'Page.evaluate: Error: Stories are not supported; '
                'use the dashboard inside it.'
            )
            raise PlaywrightError(msg)
        return None

    def close(self) -> None:
        self._closed.append(True)


class _ViewContext:
    def __init__(self, story: bool, closed: list[bool]) -> None:
        self.page = _LoadedView(story, closed)

    def new_page(self) -> _LoadedView:
        return self.page


def test_open_view_refuses_a_story_and_allows_a_dashboard(tmp_path: Path) -> None:
    closed: list[bool] = []
    settings = Settings(
        'https://tableau.example',
        'finance',
        'tabpull',
        'demo',
        tmp_path / 'auth.json',
        'pat-value',
    )
    story = _ViewContext(story=True, closed=closed)
    with pytest.raises(JobError, match='use the dashboard inside it'):
        crosstab.open_view(cast('Any', story), settings, 'Book/Story1')
    assert closed == [True]

    dashboard = _ViewContext(story=False, closed=closed)
    opened = crosstab.open_view(cast('Any', dashboard), settings, 'Book/Dash')
    assert opened is dashboard.page
    assert closed == [True]


def test_export_applies_a_blank_filter_to_the_first_sheet_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = tmp_path / 'download.csv'
    raw.write_text('a,b\n1,2\n', encoding='utf-8')
    seen: dict[str, Any] = {}

    class Download:
        def path(self) -> str:
            return str(raw)

    class Expect:
        value = Download()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Page:
        def evaluate(self, script: str, arg: object = None) -> None:
            if script == crosstab.APPLY_JS:
                seen['apply'] = arg

        def expect_download(self, timeout: int) -> Expect:
            return Expect()

        def close(self) -> None:
            return None

    monkeypatch.setattr(crosstab, 'open_view', lambda *_args, **_kwargs: Page())
    job = Job(
        'west',
        'W/V',
        ['First', 'Second'],
        'demo',
        [ValuesFilter('Region', ['West']), ValuesFilter('Year', ['2024'], 'Second')],
    )

    paths = crosstab.export_embed(
        cast('Any', object()),
        Settings(
            'https://tableau.example',
            'finance',
            'tabpull',
            'demo',
            tmp_path / 'auth.json',
            'pat-value',
        ),
        job,
        tmp_path,
    )
    note = capsys.readouterr().out

    assert seen['apply'] == {
        'filters': [
            {'field': 'Region', 'values': ['West'], 'sheet': 'First'},
            {'field': 'Year', 'values': ['2024'], 'sheet': 'Second'},
        ],
        'params': {},
    }
    assert [path.name for path in paths] == ['First.csv', 'Second.csv']
    assert "filter 'Region' names no sheet" in note
    assert 'First' in note
    assert 'Second' not in note
    assert 'pat-value' not in note


@pytest.mark.parametrize(
    'raw',
    [
        {'name': 'j', 'site': 'demo', 'method': 'rest', 'view': 'W/V', 'sheets': ['A']},
        {'name': 'j', 'site': 'demo', 'view': 'W/V', 'sheets': []},
        {'name': 'j', 'site': 'demo', 'view': 'W/V', 'sheets': ['A'], 'typo': 1},
    ],
)
def test_parse_job_rejects_invalid_jobs(raw: dict[str, object]) -> None:
    with pytest.raises(JobError, match="job 'j'"):
        parse_job(raw)


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('2024-02-29', '2024-02-29'),
        ('2/29/2024', '2024-02-29'),
        ('1/3/2023', '2023-01-03'),
        (' 2024-01-31 ', '2024-01-31'),
        ('12', '12'),
        ('-1.5', '-1.5'),
        ('', ''),
        (None, None),
    ],
)
def test_normalize_range_bound_accepts_absolute_dates_and_numbers(
    raw: str | None, expected: str | None
) -> None:
    assert crosstab.normalize_range_bound(raw) == expected


@pytest.mark.parametrize(
    'bound', ['2024-02-31', '2023-02-29', '2/31/2024', '2/30/2024']
)
def test_normalize_range_bound_rejects_an_impossible_date(bound: str) -> None:
    with pytest.raises(JobError, match='not a real date') as exc:
        crosstab.normalize_range_bound(bound)

    assert 'YYYY-MM-DD or M/D/YYYY' in str(exc.value)
    assert '2024-03-02' not in str(exc.value)


@pytest.mark.parametrize(
    'bound',
    ['yesterday', 'today', 'last week', '7 days ago', '3 days ago', 'now()', 'TODAY()'],
)
def test_normalize_range_bound_refuses_a_relative_or_runtime_date(bound: str) -> None:
    with pytest.raises(
        JobError, match='relative date or a date computed at run time'
    ) as exc:
        crosstab.normalize_range_bound(bound)

    assert 'YYYY-MM-DD or M/D/YYYY' in str(exc.value)


def test_parse_filter_spec_rejects_bad_range_bounds_and_keeps_category_words() -> None:
    with pytest.raises(JobError, match='not a real date'):
        parse_filter_spec('Order Date=2024-02-31..2024-03-01')
    with pytest.raises(JobError, match='relative date'):
        parse_filter_spec('Order Date=yesterday..today @Totals')
    assert parse_filter_spec('Ship Mode=yesterday') == ValuesFilter(
        'Ship Mode', ['yesterday'], ''
    )
    assert parse_filter_spec('Order Date=1/3/2024..2/1/2024') == RangeFilter(
        'Order Date', '', '1/3/2024', '2/1/2024'
    )


def test_export_rejects_a_bad_bound_before_opening_the_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def opened(*_args: object, **_kwargs: object) -> None:
        msg = 'opened Tableau'
        raise AssertionError(msg)

    monkeypatch.setattr(crosstab, 'open_view', opened)
    settings = Settings(
        'https://tableau.example',
        'finance',
        'tabpull',
        'demo',
        tmp_path / 'auth.json',
        'pat-value',
    )
    for bound, match in (
        ('2024-02-31', 'not a real date'),
        ('yesterday', 'relative date'),
    ):
        job = Job(
            'j',
            'W/V',
            ['A'],
            'demo',
            [RangeFilter('Order Date', 'A', bound, '2024-03-01')],
        )
        with pytest.raises(JobError, match=match):
            crosstab.export_embed(cast('Any', object()), settings, job, tmp_path)


def test_story_refusal_comes_from_one_message() -> None:
    assert crosstab.STORY_REFUSAL == (
        'Stories are not supported; use the dashboard inside it.'
    )
    guard = crosstab._STORY_GUARD_JS
    assert guard.count("sheetType === 'story'") == 1
    assert crosstab.STORY_REFUSAL in guard
    assert crosstab.STORY_JS.count(guard) == 1
    assert crosstab.INSPECT_JS.count(guard) == 1
    assert "sheetType === 'story'" not in crosstab.STORY_JS.replace(guard, '', 1)
    assert "sheetType === 'story'" not in crosstab.INSPECT_JS.replace(guard, '', 1)
