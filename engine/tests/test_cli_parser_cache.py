"""`main` builds the argument parser once per process (CI regression: the slow
episode-CLI load test fell to about 500 episodes per minute because every command
rebuilt over a hundred subparsers)."""

from __future__ import annotations

import pytest

from scruple.cli import main as cli


def test_main_builds_the_parser_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built: list[int] = []
    real = cli.build_parser

    def counting() -> object:
        built.append(1)
        return real()

    cli._parser.cache_clear()
    monkeypatch.setattr(cli, "build_parser", counting)
    try:
        for _ in range(3):
            assert cli.main(["version"]) == 0
        assert built == [1]
        assert cli._parser() is cli._parser()
    finally:
        cli._parser.cache_clear()
    assert capsys.readouterr().out.count("scruple ") == 3


def test_a_reused_parser_keeps_calls_independent(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version", "--json"]) == 0
    assert capsys.readouterr().out.startswith("{")
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.startswith("scruple ")
