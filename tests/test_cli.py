"""CLI-level behaviour, driven through click's test runner."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from suplex import cli as cli_module
from suplex.cli import cli


@pytest.fixture
def runner(store, monkeypatch) -> CliRunner:
    monkeypatch.setattr(cli_module, "LeadStore", lambda: store)
    return CliRunner()


def _all_commands(group, prefix=()):
    for name, command in group.commands.items():
        path = (*prefix, name)
        yield path
        if hasattr(command, "commands"):
            yield from _all_commands(command, path)


@pytest.mark.parametrize("path", sorted(_all_commands(cli)), ids=" ".join)
def test_every_command_has_working_help(path: tuple[str, ...]) -> None:
    result = CliRunner().invoke(cli, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_linkedin_scrape_without_token_exits_with_a_readable_error(runner, monkeypatch) -> None:
    monkeypatch.delenv("APIFY_TOKEN", raising=False)

    result = runner.invoke(cli, ["scrape", "linkedin", "dentist", "-l", "Austin, TX", "-n", "5"])

    assert result.exit_code == 1
    assert "Error: LinkedIn scraping runs on Apify and no Apify token is configured" in result.output
    assert "Traceback" not in result.output


def test_apify_maps_scrape_without_token_exits_with_a_readable_error(runner, monkeypatch) -> None:
    monkeypatch.delenv("APIFY_TOKEN", raising=False)

    result = runner.invoke(cli, ["scrape", "maps", "dentist", "-l", "Austin, TX", "-s", "apify", "-n", "5"])

    assert result.exit_code == 1
    assert "use `-s playwright`" in result.output
