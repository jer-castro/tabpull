import threading
from collections.abc import Callable
from typing import ClassVar, override

from playwright.sync_api import sync_playwright
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.content import Content
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
from textual.worker import active_worker, get_current_worker

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
ConfirmScreen #form .title { width: 1fr; }
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
            yield Static(self.message, classes='title', markup=False)
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
        return f'Failed (exit {exc.code})'
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
        self,
        title: str,
        options: list[str],
        commit: Callable[[list[str]], None],
        *,
        ticked: set[str] | None = None,
        label_suffix: dict[str, str] | None = None,
    ) -> None:
        super().__init__(title, commit)
        self._options = options
        self._ticked = ticked or set()
        self._label_suffix = label_suffix or {}

    def fields(self) -> ComposeResult:
        yield Label('Space toggles a sheet. Save keeps the ticked ones.')
        suffixes, ticked = self._label_suffix, self._ticked
        yield SelectionList[str](
            *(
                (
                    Content(f'{name}{suffixes.get(name, "")}'),
                    name,
                    name in ticked,
                )
                for name in self._options
            ),
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


_TASK_POLL_S = 0.05


class TaskScreen[T](ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('ctrl+c,escape', 'cancel', 'cancel', priority=True),
    ]

    def __init__(  # noqa: PLR0913
        self,
        title: str,
        work: Callable[[], T],
        done: Callable[[T], None],
        fail: Callable[[str], None],
        on_cancel: Callable[[], None] | None = None,
        *,
        hold_failure: bool = False,
    ) -> None:
        super().__init__()
        self.title_text = title
        self._work = work
        self._done_cb = done
        self._fail = fail
        self._on_cancel = on_cancel
        self._hold_failure = hold_failure
        self._settled = False
        self._failed_message: str | None = None
        self._call: Callable[..., object] | None = None
        self._release: Callable[[int], None] | None = None
        self._task_key = 0
        self.transcript: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id='form'):
            yield Label(self.title_text, classes='title')
            yield RichLog(id='log', markup=False, wrap=True, auto_scroll=True)
            yield Static('escape or ctrl+c cancels', id='task-hint', classes='hint')

    def on_mount(self) -> None:
        host = self.app
        self._call = host.call_from_thread
        self._task_key = id(self)
        hold = getattr(host, 'hold_task', None)
        if hold is not None:
            hold(self._task_key)
        self._release = getattr(host, 'release_task', None)
        self.run_worker(
            self._run,
            thread=True,
            exclusive=True,
            exit_on_error=False,
            group='tabpull-task',
        )

    def _run(self) -> None:
        # A blocked Tableau call must not pin process shutdown. The call runs
        # on a daemon thread; this worker returns once it is cancelled.
        worker = get_current_worker()
        finished = threading.Event()

        def work() -> None:
            token = active_worker.set(worker)
            try:
                self._work_in_thread()
            finally:
                active_worker.reset(token)
                finished.set()

        threading.Thread(target=work, name='tabpull-task', daemon=True).start()
        while not finished.wait(_TASK_POLL_S):
            if worker.is_cancelled:
                return

    def _work_in_thread(self) -> None:
        call = self._call
        if call is None:
            return

        def log(text: str) -> None:
            call(self._write, text)

        def stop() -> bool:
            return get_current_worker().is_cancelled

        try:
            try:
                with ui.capture(log, stop):
                    result = self._work()
            except (KeyboardInterrupt, SystemExit, Exception) as exc:  # noqa: BLE001
                self._drop_task()
                call(self._fail_with, _failure_text(exc))
                return
            self._drop_task()
            call(self._succeed, result)
        finally:
            self._drop_task()

    def _drop_task(self) -> None:
        release = self._release
        key = self._task_key
        if release is None or key == 0:
            return
        self._task_key = 0
        release(key)

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
        if self._settled or not self.is_attached:
            return
        if not self._hold_failure:
            self._settled = True
            self.dismiss()
            self._fail(message)
            return
        if self._failed_message is not None:
            return
        self._failed_message = message
        self._write(message)
        self.query_one('#task-hint', Static).update('press escape to close')

    def action_cancel(self) -> None:
        if self._failed_message is not None:
            message = self._failed_message
            self._failed_message = None
            self._settled = True
            self.dismiss()
            self._fail(message)
            return
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


def sign_in(settings: Settings) -> str:
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
        yield Label('Token name (blank keeps the saved name)')
        yield Input(placeholder='tabpull', id='pat-name')
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
        secret = self.text('pat-secret').strip()
        current: Settings | None = None
        try:
            current = load_site(name)
        except (UnknownSiteError, MissingSettingsError, ValueError):
            current = None
        token = self.text('pat-name').strip() or (
            current.pat_name if current is not None and current.pat_name else 'tabpull'
        )
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
                    f'{message}\n\nSave this token anyway?',
                    lambda: self._checked(settings),
                )
            )

        _start_task(
            self,
            TaskScreen(
                'Checking the token…',
                lambda: _check_token(settings),
                lambda _ok: self._checked(settings),
                failed,
            ),
        )

    def _checked(self, settings: Settings) -> None:
        try:
            save_site(
                settings.name,
                {
                    'TABLEAU_SERVER_URL': settings.server,
                    'TABLEAU_SITE': settings.site,
                    'TABLEAU_PAT_NAME': settings.pat_name,
                    'TABLEAU_PAT_SECRET': settings.pat_secret,
                },
            )
        except OSError as e:
            _show_error(self, str(e))
            return
        if self.query_one('#sso', Checkbox).value:
            _start_task(
                self,
                TaskScreen(
                    'Signing in…',
                    lambda: sign_in(settings),
                    lambda _name: self._finish(settings.name),
                    lambda message: self._finish(settings.name, error=message),
                ),
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
        elif skipped:
            self.app.notify(f'Site {name} saved. Browser sign-in skipped.')
        else:
            self.app.notify(f'Site {name} saved and signed in')
        if self._on_done is not None:
            self._on_done(name)


def _start_task[T](screen: Widget, task: TaskScreen[T]) -> None:
    start = getattr(screen.app, 'start_task', None)
    if start is None:
        screen.app.push_screen(task)
        return
    start(task)
