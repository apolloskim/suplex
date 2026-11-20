from __future__ import annotations

import stat

import pytest

from suplex import config as config_module
from suplex.config import Config, ConfigError


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "suplex" / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    for env_name in config_module.ENV_MAP.values():
        monkeypatch.delenv(env_name, raising=False)
    return path


def test_save_and_load_round_trip_values_with_special_characters(config_path) -> None:
    tricky = 'abc"def\\ghi'
    Config(apify_token=tricky, anthropic_api_key="sk-test").save()

    loaded = Config.load()

    assert loaded.apify_token == tricky
    assert loaded.anthropic_api_key == "sk-test"
    assert loaded.reacher_api_key == ""


def test_config_file_is_private_to_the_user(config_path) -> None:
    Config(apify_token="secret").save()

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_save_tightens_permissions_of_an_existing_file(config_path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text('apify_token = "old"\n')
    config_path.chmod(0o644)

    Config(apify_token="new").save()

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_env_vars_override_the_file(config_path, monkeypatch) -> None:
    Config(apify_token="from-file").save()
    monkeypatch.setenv("APIFY_TOKEN", "from-env")

    assert Config.load().apify_token == "from-env"


def test_invalid_toml_reports_the_file_instead_of_a_traceback(config_path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("apify_token = unquoted\n")

    with pytest.raises(ConfigError, match="config.toml is not valid TOML"):
        Config.load()
