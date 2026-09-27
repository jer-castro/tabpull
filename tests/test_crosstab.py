import tomllib
from pathlib import Path
from typing import Any, Self, cast

import pytest
from playwright.sync_api import Error as PlaywrightError

from tabpull import add, embed
from tabpull.add import match_score, view_from_flag
from tabpull.embed import normalize_csv
from tabpull.filters import (
    filters_for_run,
    normalize_range_bound,
    parse_filter_spec,
    resolved_filters,
)
from tabpull.jobs import (
    Job,
    JobError,
    RangeFilter,
    ValuesFilter,
    job_to_toml,
    parse_job,
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
            RangeFilter('Ship Date', 'Detail Table', None, '2026-02-01'),
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

    assert (match_score(query, text) >= add.MIN_MATCH_SCORE) is matches


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
        ('Order Date=..2026-09-25', RangeFilter('Order Date', '', None, '2026-09-25')),
        (
            'Order Date=..2026-09-25 @Totals',
            RangeFilter('Order Date', 'Totals', None, '2026-09-25'),
        ),
        ('Amount=10..', RangeFilter('Amount', '', '10', None)),
    ],
)
def test_parse_filter_spec(spec: str, expected: ValuesFilter | RangeFilter) -> None:
    assert parse_filter_spec(spec) == expected


def test_parse_filter_spec_rejects_a_bare_word() -> None:
    with pytest.raises(JobError, match='Field='):
        parse_filter_spec('Region')


@pytest.mark.parametrize('spec', ['Order Date=..', 'Order Date= .. @Totals'])
def test_parse_filter_spec_rejects_a_range_with_both_sides_empty(spec: str) -> None:
    with pytest.raises(JobError, match=r'Field=min\.\.max'):
        parse_filter_spec(spec)


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
        if script != embed.STORY_JS:
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
        embed.open_view(cast('Any', story), settings, 'Book/Story1')
    assert closed == [True]

    dashboard = _ViewContext(story=False, closed=closed)
    opened = embed.open_view(cast('Any', dashboard), settings, 'Book/Dash')
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
            if script == embed.APPLY_JS:
                seen['apply'] = arg

        def expect_download(self, timeout: int) -> Expect:
            return Expect()

        def close(self) -> None:
            return None

    monkeypatch.setattr(embed, 'open_view', lambda *_args, **_kwargs: Page())
    job = Job(
        'west',
        'W/V',
        ['First', 'Second'],
        'demo',
        [ValuesFilter('Region', ['West']), ValuesFilter('Year', ['2024'], 'Second')],
    )

    paths = embed.export_embed(
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
        ('', None),
        (None, None),
    ],
)
def test_normalize_range_bound_accepts_absolute_dates_and_numbers(
    raw: str | None, expected: str | None
) -> None:
    assert normalize_range_bound(raw) == expected


@pytest.mark.parametrize(
    'bound', ['2024-02-31', '2023-02-29', '2/31/2024', '2/30/2024']
)
def test_normalize_range_bound_rejects_an_impossible_date(bound: str) -> None:
    with pytest.raises(JobError, match='not a real date') as exc:
        normalize_range_bound(bound)

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
        normalize_range_bound(bound)

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

    monkeypatch.setattr(embed, 'open_view', opened)
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
            embed.export_embed(cast('Any', object()), settings, job, tmp_path)


def test_story_refusal_comes_from_one_message() -> None:
    assert embed.STORY_REFUSAL == (
        'Stories are not supported; use the dashboard inside it.'
    )
    guard = embed._STORY_GUARD_JS
    assert guard.count("sheetType === 'story'") == 1
    assert embed.STORY_REFUSAL in guard
    assert embed.STORY_JS.count(guard) == 1
    assert embed.INSPECT_JS.count(guard) == 1
    assert "sheetType === 'story'" not in embed.STORY_JS.replace(guard, '', 1)
    assert "sheetType === 'story'" not in embed.INSPECT_JS.replace(guard, '', 1)


def test_filter_payload_leaves_an_omitted_bound_unset() -> None:
    payload = embed.filter_payload(
        RangeFilter('Order Date', 'Totals', '1/3/2024', None)
    )

    assert payload['min'] == '2024-01-03'
    assert payload['max'] is None
    assert embed.filter_payload(
        RangeFilter('Order Date', 'Totals', None, '2/1/2024')
    ) == {
        'field': 'Order Date',
        'sheet': 'Totals',
        'min': None,
        'max': '2024-02-01',
    }


def test_jobs_file_range_with_a_blank_bound_leaves_that_end_open() -> None:
    job = parse_job(
        tomllib.loads(
            """
            [[job]]
            name = "daily"
            site = "demo"
            view = "W/V"
            sheets = ["Totals"]
            filters = [{ field = "Order Date", min = "", max = "2/1/2024" }]
            """
        )['job'][0]
    )

    assert embed.filter_payload(job.filters[0])['min'] is None


@pytest.mark.parametrize(
    'bounds',
    ['', 'min = "", ', 'min = " ", max = "", '],
)
def test_jobs_file_rejects_a_range_with_no_bounds(bounds: str) -> None:
    raw = tomllib.loads(
        f"""
        [[job]]
        name = "daily"
        site = "demo"
        view = "W/V"
        sheets = ["Totals"]
        filters = [{{ {bounds}field = "Order Date", sheet = "Totals" }}]
        """
    )['job'][0]

    with pytest.raises(JobError, match=r"'daily'.*needs a min, a max, or both"):
        parse_job(raw)


@pytest.mark.parametrize('bound', ['min = 2024-01-01', 'max = 5'])
def test_jobs_file_asks_to_quote_an_unquoted_range_bound(bound: str) -> None:
    jobs = f"""
        [[job]]
        name = "daily"
        site = "demo"
        view = "W/V"
        sheets = ["Totals"]
        filters = [{{ field = "Order Date", sheet = "Totals", {bound} }}]
        """

    with pytest.raises(JobError, match=r"'daily'.*quote min and max"):
        parse_job(tomllib.loads(jobs)['job'][0])


def test_filters_for_run_replaces_the_same_field_and_sheet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = Job(
        'daily',
        'W/V',
        ['Totals', 'Detail'],
        'demo',
        [
            RangeFilter('Order Date', 'Totals', '2026-09-01', '2026-09-25'),
            ValuesFilter('Region', ['West']),
        ],
    )

    updated = filters_for_run(
        job,
        [
            parse_filter_spec('Order Date=2026-10-01..'),
            parse_filter_spec('Ship Mode=First Class @Detail'),
            parse_filter_spec('Order Date=..2026-08-01 @Detail'),
        ],
    )
    note = capsys.readouterr().out

    assert updated.filters == [
        RangeFilter('Order Date', 'Totals', '2026-10-01', None),
        ValuesFilter('Region', ['West'], ''),
        ValuesFilter('Ship Mode', ['First Class'], 'Detail'),
        RangeFilter('Order Date', 'Detail', None, '2026-08-01'),
    ]
    assert not note
    assert job.filters == [
        RangeFilter('Order Date', 'Totals', '2026-09-01', '2026-09-25'),
        ValuesFilter('Region', ['West']),
    ]


def test_filters_for_run_matches_a_blank_sheet_on_the_first_sheet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = Job(
        'daily',
        'W/V',
        ['A', 'B'],
        'demo',
        [RangeFilter('Order Date', '', '2020-01-01', '2020-02-01')],
    )

    replaced = filters_for_run(
        job, [parse_filter_spec('Order Date=2024-01-01..2024-01-31 @A')]
    )
    added = filters_for_run(job, [parse_filter_spec('Order Date=2024-01-01.. @B')])

    assert replaced.filters == [
        RangeFilter('Order Date', 'A', '2024-01-01', '2024-01-31')
    ]
    assert added.filters == [
        RangeFilter('Order Date', '', '2020-01-01', '2020-02-01'),
        RangeFilter('Order Date', 'B', '2024-01-01', None),
    ]
    assert not capsys.readouterr().out


def test_filters_for_run_without_a_sheet_replaces_the_field_on_every_sheet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = Job(
        'daily',
        'W/V',
        ['Totals', 'Detail', 'Map'],
        'demo',
        [
            RangeFilter('Order Date', 'Detail', '2026-06-01', '2026-06-30'),
            ValuesFilter('Region', ['West'], 'Detail'),
            RangeFilter('Order Date', 'Map', '2026-06-01', '2026-06-30'),
        ],
    )

    everywhere = filters_for_run(job, [parse_filter_spec('Order Date=2026-09-01..')])
    only_map = filters_for_run(job, [parse_filter_spec('Order Date=..2026-09-25 @Map')])

    assert everywhere.filters == [
        RangeFilter('Order Date', 'Detail', '2026-09-01', None),
        ValuesFilter('Region', ['West'], 'Detail'),
        RangeFilter('Order Date', 'Map', '2026-09-01', None),
    ]
    assert only_map.filters == [
        RangeFilter('Order Date', 'Detail', '2026-06-01', '2026-06-30'),
        ValuesFilter('Region', ['West'], 'Detail'),
        RangeFilter('Order Date', 'Map', None, '2026-09-25'),
    ]
    assert not capsys.readouterr().out
