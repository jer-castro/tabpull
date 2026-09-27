import sys

import pytest

from tabpull import cli


def test_version_flag_answers_without_loading_app(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delitem(sys.modules, 'tabpull.app', raising=False)
    monkeypatch.setattr(sys, 'argv', ['tabpull', '--version'])
    cli.main()
    assert capsys.readouterr().out == f'{cli.version()}\n'
    assert 'tabpull.app' not in sys.modules


def test_other_args_reach_app(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, 'argv', ['tabpull', '--help'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert 'Commands:' in capsys.readouterr().out
