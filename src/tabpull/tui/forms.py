from collections.abc import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
    TextArea,
)

from tabpull.filters import make_filter
from tabpull.jobs import JobError, RangeFilter, ValuesFilter

FORM_CSS = """
Form, ConfirmScreen { align: center middle; }
#form {
    width: 72; height: auto; max-height: 90%;
    border: round $accent; background: $surface; padding: 1 2;
}
#form .title { text-style: bold; margin-bottom: 1; }
#form > Vertical { height: auto; }
#form Input, #form RadioSet, #form TextArea { margin-bottom: 1; }
#form TextArea { height: 8; }
#error { color: $error; height: auto; display: none; }
#form .hint { color: $text-muted; }
#form .buttons { height: auto; }
#form Button { margin-right: 2; }
"""


class Form[T](ModalScreen[T | None]):
    """Modal that builds a value, hands it to `commit`, and closes on success.

    `commit` raises JobError to keep the form open with the message shown.
    """

    BINDINGS = [
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


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding('y', 'answer(True)', 'yes'),
        Binding('n,escape', 'answer(False)', 'no'),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id='form'):
            yield Label(self.message, classes='title')
            with Horizontal(classes='buttons'):
                yield Button('Yes (y)', variant='error', id='yes')
                yield Button('No (n)', id='no')

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == 'yes')

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)
