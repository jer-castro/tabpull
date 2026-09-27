from collections.abc import Callable
from typing import ClassVar, override

from playwright.sync_api import sync_playwright
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    RichLog,
    SelectionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from tabpull import ui
from tabpull.filters import make_filter
from tabpull.jobs import JobError, RangeFilter, ValuesFilter
from tabpull.tableau import (
    MissingSettingsError,
    Settings,
    UnknownSiteError,
    check_site_name,
    home_url,
    load_site,
    parse_tableau_url,
    rest_session,
    save_site,
    site_auth_path,
    sso_login,
)
from tabpull.wizard import launch_url

FORM_CSS = """
Form, ConfirmScreen, ChoiceScreen, PickScreen, TaskScreen { align: center middle; }
#form {
    width: 72; height: auto; max-height: 90%;
    border: round $accent; background: $surface; padding: 1 2;
}
#form .title { text-style: bold; margin-bottom: 1; }
#form > Vertical { height: auto; }
#form Input, #form RadioSet, #form TextArea, #form Checkbox,
#form SelectionList, #form OptionList { margin-bottom: 1; }
#form TextArea { height: 8; }
#form SelectionList, #form OptionList { height: auto; max-height: 16; }
#error { color: $error; height: auto; display: none; }
#form .hint { color: $text-muted; }
#form .buttons { height: auto; }
#form Button { margin-right: 2; }
ChoiceScreen #form, PickScreen #form { width: 100; }
TaskScreen #form { height: 22; width: 80; }
TaskScreen #log { height: 1fr; margin-bottom: 1; }
"""


class Form[T](ModalScreen[T | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('escape', 'cancel', 'cancel'),
        Binding('ctrl+s', 'submit', 'save'),
    ]

    def __init__(self, title: str, commit: Callable[[T], None]) -> None:
        super().__init__()
        self.title_text = title
        self.commit = commit

    def fields(self) -> ComposeResult:
        raise NotImplementedError

    def build(self) -> T:
        raise NotImplementedError

    def compose(self) -> ComposeResult:
        with Vertical(id='form'):
            yield Label(self.title_text, classes='title')
            yield from self.fields()
            yield Static('', id='error')
            with Horizontal(classes='buttons'):
                yield Button('Save', variant='primary', id='save')
                yield Button('Cancel', id='cancel')

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'save':
            self.action_submit()
        else:
            self.action_cancel()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.action_submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        try:
            value = self.build()
            self.commit(value)
        except JobError as e:
            error = self.query_one('#error', Static)
            error.update(str(e))
            error.display = True
            return
        self.dismiss(value)

    def text(self, widget_id: str) -> str:
        return self.query_one(f'#{widget_id}', Input).value


class TextForm(Form[str]):
    def __init__(
        self, title: str, label: str, value: str, commit: Callable[[str], None]
    ) -> None:
        super().__init__(title, commit)
        self.label, self.value = label, value

    def fields(self) -> ComposeResult:
        yield Label(self.label)
        yield Input(self.value, id='value')

    def build(self) -> str:
        value = self.text('value').strip()
        if not value:
            msg = f'{self.label} is empty'
            raise JobError(msg)
        return value


class ParamForm(Form[tuple[str, str]]):
    def __init__(
        self,
        title: str,
        name: str,
        value: str,
        commit: Callable[[tuple[str, str]], None],
    ) -> None:
        super().__init__(title, commit)
        self.param_name, self.value = name, value

    def fields(self) -> ComposeResult:
        yield Label('Parameter name (as Tableau shows it)')
        yield Input(self.param_name, id='name')
        yield Label('Value')
        yield Input(self.value, id='value')

    def build(self) -> tuple[str, str]:
        name = self.text('name').strip()
        if not name:
            msg = 'parameter needs a name'
            raise JobError(msg)
        return name, self.text('value').strip()


class SheetsForm(Form[list[str]]):
    def __init__(
        self, title: str, sheets: list[str], commit: Callable[[list[str]], None]
    ) -> None:
        super().__init__(title, commit)
        self.sheets = sheets

    def fields(self) -> ComposeResult:
        yield Label('One worksheet per line, exactly as named in the dashboard')
        yield TextArea('\n'.join(self.sheets), id='sheets')
        yield Label(
            'Run `tabpull add` to pick sheets from the live view instead.',
            classes='hint',
        )

    def build(self) -> list[str]:
        text = self.query_one('#sheets', TextArea).text
        sheets = [line.strip() for line in text.splitlines() if line.strip()]
        if not sheets:
            msg = 'needs at least one sheet'
            raise JobError(msg)
        return sheets


class FilterForm(Form[ValuesFilter | RangeFilter]):
    def __init__(
        self,
        title: str,
        item: ValuesFilter | RangeFilter | None,
        default_sheet: str,
        commit: Callable[[ValuesFilter | RangeFilter], None],
    ) -> None:
        super().__init__(title, commit)
        self.item = item
        self.default_sheet = default_sheet

    def fields(self) -> ComposeResult:
        item = self.item
        is_range = item is not None and item.kind == RangeFilter.kind
        yield Label('Field (as Tableau shows it)')
        yield Input(item.field if item else '', id='field')
        yield Label('Sheet the filter is set on')
        yield Input(item.sheet if item else self.default_sheet, id='sheet')
        with RadioSet(id='kind'):
            yield RadioButton(
                'Values (pick list)', value=not is_range, id='values-kind'
            )
            yield RadioButton(
                'Range (dates or numbers)', value=is_range, id='range-kind'
            )
        with Vertical(id='values-box'):
            yield Label('Values, separated by |')
            yield Input(item.pick_list if item else '', id='values')
        low, high = item.bounds if item else ('', '')
        with Vertical(id='range-box'):
            yield Label('From (YYYY-MM-DD, M/D/YYYY, or a number; blank = open)')
            yield Input(low, id='low')
            yield Label('To (blank = open)')
            yield Input(high, id='high')

    def on_mount(self) -> None:
        self._show_kind()

    def on_radio_set_changed(self, _event: RadioSet.Changed) -> None:
        self._show_kind()

    def _is_range(self) -> bool:
        return self.query_one('#range-kind', RadioButton).value

    def _show_kind(self) -> None:
        is_range = self._is_range()
        self.query_one('#values-box').display = not is_range
        self.query_one('#range-box').display = is_range

    def build(self) -> ValuesFilter | RangeFilter:
        field_name, sheet = self.text('field'), self.text('sheet')
        if self._is_range():
            return make_filter(
                field_name, sheet, low=self.text('low'), high=self.text('high')
            )
        return make_filter(field_name, sheet, values=self.text('values'))


class ConfirmScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('y', 'yes', 'yes'),
        Binding('n,escape', 'no', 'no'),
    ]

    def __init__(self, message: str, on_yes: Callable[[], None]) -> None:
        super().__init__()
        self.message = message
        self.on_yes = on_yes

    def compose(self) -> ComposeResult:
        with Vertical(id='form'):
            yield Label(self.message, classes='title')
            with Horizontal(classes='buttons'):
                yield Button('Yes (y)', variant='error', id='yes')
                yield Button('No (n)', id='no')

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'yes':
            self.action_yes()
        else:
            self.action_no()

    def action_yes(self) -> None:
        self.dismiss()
        self.on_yes()

    def action_no(self) -> None:
        self.dismiss()


def _failure_text(exc: BaseException) -> str:
    if isinstance(exc, KeyboardInterrupt):
        return 'Cancelled.'
    if isinstance(exc, SystemExit):
        if isinstance(exc.code, str) and exc.code:
            return exc.code
        return 'Cancelled.'
    text = str(exc).partition('\n')[0].strip()
    return text or exc.__class__.__name__


def _show_error(screen: Widget, message: str) -> None:
    error = screen.query_one('#error', Static)
    error.update(message)
    error.display = True


class ChoiceScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('enter', 'pick', 'pick', priority=True),
        Binding('escape', 'cancel', 'cancel'),
    ]

    def __init__(
        self,
        title: str,
        options: list[tuple[str, str]],
        commit: Callable[[str], None],
        *,
        disabled: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self._options = options
        self._commit = commit
        self._disabled = disabled or set()

    def compose(self) -> ComposeResult:
        with Vertical(id='form'):
            yield Label(self.title_text, classes='title')
            yield OptionList(
                *[
                    Option(label, id=value, disabled=value in self._disabled)
                    for label, value in self._options
                ],
                id='choices',
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def action_pick(self) -> None:
        self.query_one(OptionList).action_select()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option = event.option
        if option.id is None or option.disabled:
            return
        self.dismiss()
        self._commit(option.id)

    def action_cancel(self) -> None:
        self.dismiss()


class PickScreen(ModalScreen[None]):
    """A list that stays open until Done. Enter on a row calls on_pick."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('enter', 'pick', 'pick', priority=True),
        Binding('escape', 'cancel', 'cancel'),
    ]

    def __init__(
        self,
        title: str,
        options: list[tuple[str, str]],
        on_pick: Callable[[str], None],
        on_done: Callable[[], None],
    ) -> None:
        super().__init__()
        self.title_text = title
        self._options = options
        self._on_pick = on_pick
        self._on_done = on_done

    def compose(self) -> ComposeResult:
        with Vertical(id='form'):
            yield Label(self.title_text, classes='title')
            yield Static(
                'Enter picks a row. Done moves on. Escape cancels.', classes='hint'
            )
            yield OptionList(
                *[Option(label, id=value) for label, value in self._options],
                Option('Done', id='done'),
                id='choices',
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def action_pick(self) -> None:
        self.query_one(OptionList).action_select()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == 'done':
            self.dismiss()
            self._on_done()
            return
        if event.option.id is None:
            return
        self._on_pick(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss()


class ChecksForm(Form[list[str]]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('space', 'tick', 'tick', priority=True),
    ]

    def __init__(
        self, title: str, options: list[str], commit: Callable[[list[str]], None]
    ) -> None:
        super().__init__(title, commit)
        self._options = options

    def fields(self) -> ComposeResult:
        yield Label('Space toggles a sheet. Save keeps the ticked ones.')
        yield SelectionList[str](
            *((name, name, False) for name in self._options),
            id='checks',
        )

    def on_mount(self) -> None:
        self.query_one(SelectionList).focus()

    def action_tick(self) -> None:
        self.query_one(SelectionList).action_select()

    def build(self) -> list[str]:
        picked = set(self.query_one(SelectionList).selected)
        chosen = [name for name in self._options if name in picked]
        if not chosen:
            msg = 'Pick at least one sheet'
            raise JobError(msg)
        return chosen


class TaskScreen[T](ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('ctrl+c,escape', 'cancel', 'cancel', priority=True),
    ]

    def __init__(
        self,
        title: str,
        work: Callable[[], T],
        done: Callable[[T], None],
        fail: Callable[[str], None],
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self._work = work
        self._done_cb = done
        self._fail = fail
        self._on_cancel = on_cancel
        self._settled = False
        self.transcript: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id='form'):
            yield Label(self.title_text, classes='title')
            yield RichLog(id='log', markup=False, wrap=True, auto_scroll=True)
            yield Static('escape or ctrl+c cancels', classes='hint')

    def on_mount(self) -> None:
        self.run_worker(
            self._run,
            thread=True,
            exclusive=True,
            exit_on_error=False,
            group='tabpull-task',
        )

    def _run(self) -> None:
        def log(text: str) -> None:
            self.app.call_from_thread(self._write, text)

        def stop() -> bool:
            return get_current_worker().is_cancelled

        try:
            with ui.capture(log, stop):
                result = self._work()
        except (KeyboardInterrupt, SystemExit, Exception) as exc:  # noqa: BLE001
            self.app.call_from_thread(self._fail_with, _failure_text(exc))
            return
        self.app.call_from_thread(self._succeed, result)

    def _write(self, text: str) -> None:
        if self._settled or not self.is_attached:
            return
        log = self.query_one(RichLog)
        for line in text.splitlines():
            if line:
                self.transcript.append(line)
                log.write(line)

    def _succeed(self, result: T) -> None:
        if self._settled:
            return
        self._settled = True
        self.dismiss()
        self._done_cb(result)

    def _fail_with(self, message: str) -> None:
        if self._settled:
            return
        self._settled = True
        self.dismiss()
        self._fail(message)

    def action_cancel(self) -> None:
        if self._settled:
            return
        self._settled = True
        self.workers.cancel_group(self, 'tabpull-task')
        self.dismiss()
        if self._on_cancel is None:
            self.app.notify('Cancelled.', severity='warning')
            return
        self._on_cancel()


def _check_token(settings: Settings) -> str:
    with rest_session(settings):
        return 'ok'


def _sign_in(settings: Settings) -> str:
    with sync_playwright() as playwright:
        sso_login(playwright, settings)
    return settings.name


class SetupForm(Form[None]):
    def __init__(self, on_done: Callable[[str], None] | None = None) -> None:
        super().__init__('Set up a Tableau site', lambda _value: None)
        self._on_done = on_done

    @override
    def fields(self) -> ComposeResult:
        yield Static(
            'Create a personal access token on the site, then paste it here. '
            'Browser sign-in is separate and saves the session exports use.',
            classes='hint',
        )
        yield Label('Local site name')
        yield Input(placeholder='finance', id='site')
        yield Label('Dashboard URL (blank keeps the saved URL)')
        yield Input(placeholder='https://...', id='url')
        yield Label('Token name')
        yield Input('tabpull', id='pat-name')
        yield Label('Token secret (blank keeps the saved secret)')
        yield Input(password=True, id='pat-secret')
        yield Checkbox('Sign in with the browser after saving', value=True, id='sso')
        yield Button('Open account page', id='open-account')

    def on_mount(self) -> None:
        self.query_one('#site', Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'open-account':
            self._open_account()
            return
        super().on_button_pressed(event)

    @override
    def build(self) -> None:
        return None

    def _open_account(self) -> None:
        try:
            parsed = parse_tableau_url(self.text('url'))
        except ValueError as e:
            _show_error(self, str(e))
            return
        if not launch_url(home_url(parsed.server, parsed.site)):
            _show_error(
                self, 'Could not open a browser. Open the dashboard URL yourself.'
            )

    def _collect(self) -> Settings:
        name = check_site_name(self.text('site').strip())
        url = self.text('url').strip()
        token = self.text('pat-name').strip() or 'tabpull'
        secret = self.text('pat-secret').strip()
        current: Settings | None = None
        try:
            current = load_site(name)
        except (UnknownSiteError, MissingSettingsError, ValueError):
            current = None
        if url:
            parsed = parse_tableau_url(url)
            server, site = parsed.server, parsed.site
        elif current is not None:
            server, site = current.server, current.site
        else:
            msg = 'Dashboard URL is empty'
            raise JobError(msg)
        if not secret:
            if current is None or not current.pat_secret:
                msg = 'Token secret is empty'
                raise JobError(msg)
            secret = current.pat_secret
        return Settings(server, site, token, name, site_auth_path(name), secret)

    def action_submit(self) -> None:
        try:
            settings = self._collect()
        except (JobError, ValueError) as e:
            _show_error(self, str(e))
            return

        def failed(message: str) -> None:
            self.app.push_screen(
                ConfirmScreen(
                    f'{message} Save this token anyway?',
                    lambda: self._checked(settings),
                )
            )

        self.app.push_screen(
            TaskScreen(
                'Checking the token…',
                lambda: _check_token(settings),
                lambda _ok: self._checked(settings),
                failed,
            )
        )

    def _checked(self, settings: Settings) -> None:
        save_site(
            settings.name,
            {
                'TABLEAU_SERVER_URL': settings.server,
                'TABLEAU_SITE': settings.site,
                'TABLEAU_PAT_NAME': settings.pat_name,
                'TABLEAU_PAT_SECRET': settings.pat_secret,
            },
        )
        if self.query_one('#sso', Checkbox).value:
            self.app.push_screen(
                TaskScreen(
                    'Signing in…',
                    lambda: _sign_in(settings),
                    lambda _name: self._finish(settings.name),
                    lambda message: self._finish(settings.name, error=message),
                )
            )
            return
        self._finish(settings.name, skipped=True)

    def _finish(self, name: str, *, error: str = '', skipped: bool = False) -> None:
        self.dismiss()
        if error:
            self.app.notify(
                f'{name} saved, but sign-in failed: {error}',
                severity='error',
                timeout=10,
            )
            return
        if skipped:
            self.app.notify(f'Site {name} saved. Browser sign-in skipped.')
        else:
            self.app.notify(f'Site {name} saved and signed in')
        if self._on_done is not None:
            self._on_done(name)
