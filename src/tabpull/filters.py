import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from tabpull.jobs import Job, JobError, RangeFilter, ValuesFilter

# Tableau's range-filter API only accepts a Date or a number.
_US_DATE = re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})$')
_ISO_DATE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
_NUMBER = re.compile(r'^-?\d+(\.\d+)?$')


def resolved_filters(job: Job) -> list[ValuesFilter | RangeFilter]:
    if not job.sheets:
        msg = f'job {job.name!r}: needs at least one sheet'
        raise JobError(msg)
    default = job.sheets[0]
    resolved: list[ValuesFilter | RangeFilter] = []
    for item in job.filters:
        if item.sheet:
            resolved.append(item)
            continue
        _note_default_sheet(job, item.field, default)
        resolved.append(replace(item, sheet=default))
    return resolved


def _note_default_sheet(job: Job, field_name: str, sheet: str) -> None:
    print(
        f'  {job.name}: filter {field_name!r} names no sheet; '
        f'applying it on {sheet!r}, the first sheet in the job.'
    )


def _overrides(
    item: ValuesFilter | RangeFilter,
    existing: ValuesFilter | RangeFilter,
    default_sheet: str,
) -> bool:
    return existing.field == item.field and (
        not item.sheet or (existing.sheet or default_sheet) == item.sheet
    )


def filters_for_run(job: Job, overrides: Sequence[ValuesFilter | RangeFilter]) -> Job:
    if not overrides:
        return job
    default = job.sheets[0]
    filters: list[ValuesFilter | RangeFilter] = list(job.filters)
    for item in overrides:
        if any(_overrides(item, existing, default) for existing in filters):
            filters = [
                replace(item, sheet=item.sheet or existing.sheet)
                if _overrides(item, existing, default)
                else existing
                for existing in filters
            ]
            continue
        if not item.sheet:
            _note_default_sheet(job, item.field, default)
        filters.append(replace(item, sheet=item.sheet or default))
    return replace(job, filters=filters)


def params_for_run(job: Job, overrides: Sequence[tuple[str, str]]) -> Job:
    if not overrides:
        return job
    return replace(job, params={**job.params, **dict(overrides)})


def _calendar_day(year: int, month: int, day: int, original: str) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        msg = f'{original!r} is not a real date; use YYYY-MM-DD or M/D/YYYY'
        raise JobError(msg) from None


def normalize_range_bound(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _NUMBER.fullmatch(text):
        return text
    us = _US_DATE.fullmatch(text)
    if us is not None:
        month, day, year = (int(part) for part in us.groups())
        return _calendar_day(year, month, day, value).isoformat()
    iso = _ISO_DATE.fullmatch(text)
    if iso is not None:
        year, month, day = (int(part) for part in iso.groups())
        return _calendar_day(year, month, day, value).isoformat()
    msg = (
        f'{value!r} is a relative date or a date computed at run time; '
        'use YYYY-MM-DD or M/D/YYYY'
    )
    raise JobError(msg)


def split_values(value: str) -> list[str]:
    return [v.strip() for v in value.split('|')]


def parse_filter_spec(spec: str) -> ValuesFilter | RangeFilter:
    body, sep, sheet = spec.rpartition(' @')
    if not sep:
        body, sheet = spec, ''
    field_name, eq, value = body.partition('=')
    field_name = field_name.strip()
    sheet = sheet.strip()
    if not eq or not field_name:
        msg = (
            f'filter {spec!r} should look like Field=a|b or Field=min..max, '
            'with an optional " @Sheet"'
        )
        raise JobError(msg)
    if '..' in value:
        low, _, high = value.partition('..')
        if not low.strip() and not high.strip():
            msg = (
                f'filter {spec!r} should look like Field=a|b or Field=min..max, '
                'with an optional " @Sheet"'
            )
            raise JobError(msg)
        return make_filter(field_name, sheet, low=low, high=high)
    return make_filter(field_name, sheet, values=value)


def make_filter(
    field_name: str,
    sheet: str,
    *,
    values: str | None = None,
    low: str | None = None,
    high: str | None = None,
) -> ValuesFilter | RangeFilter:
    field_name, sheet = field_name.strip(), sheet.strip()
    if not field_name:
        msg = 'filter needs a field name'
        raise JobError(msg)
    if values is not None:
        return ValuesFilter(field_name, split_values(values), sheet)
    low, high = (low or '').strip() or None, (high or '').strip() or None
    if low is None and high is None:
        msg = f'range filter {field_name!r} needs a min, a max, or both'
        raise JobError(msg)
    normalize_range_bound(low)
    normalize_range_bound(high)
    return RangeFilter(field_name, sheet, low, high)


def format_filter(item: ValuesFilter | RangeFilter) -> str:
    spec = f'{item.field}={item.shown}'
    return f'{spec} @{item.sheet}' if item.sheet else spec


def parse_param_spec(spec: str) -> tuple[str, str]:
    key, sep, value = spec.partition('=')
    if not sep or not key.strip():
        msg = f'param {spec!r} should look like Name=value'
        raise JobError(msg)
    return key.strip(), value.strip()
