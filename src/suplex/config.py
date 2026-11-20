"""Configuration — reads from ~/.config/suplex/config.toml, env vars override."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "suplex" / "config.toml"

# Map config field names to env var names.
ENV_MAP = {
    "apify_token": "APIFY_TOKEN",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "reacher_api_key": "REACHER_API_KEY",
    "reacher_url": "REACHER_URL",
    "million_verifier_api_key": "MILLION_VERIFIER_API_KEY",
}


class ConfigError(ValueError):
    """The config file exists but cannot be used."""


@dataclass
class Config:
    apify_token: str = ""
    anthropic_api_key: str = ""
    reacher_api_key: str = ""
    reacher_url: str = ""
    million_verifier_api_key: str = ""

    def is_configured(self, key: str) -> bool:
        return bool(getattr(self, key, ""))

    def save(self) -> None:
        """Write the config file, readable by the current user only (it holds API keys)."""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Suplex configuration"]
        for f in fields(self):
            val = getattr(self, f.name)
            if val:
                # A JSON string literal is also a valid TOML basic string, so
                # quotes and backslashes in a value cannot corrupt the file.
                lines.append(f"{f.name} = {json.dumps(val)}")

        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        # os.open only applies the mode to new files; tighten ones written by older versions.
        os.chmod(CONFIG_PATH, 0o600)

    @classmethod
    def load(cls) -> Config:
        """Load config from file, then overlay env vars."""
        data: dict[str, str] = {}

        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(
                    f"{CONFIG_PATH} is not valid TOML ({exc}). Fix or delete it, then run `suplex setup`."
                ) from exc

        # Env vars override file values.
        for field_name, env_name in ENV_MAP.items():
            env_val = os.environ.get(env_name)
            if env_val:
                data[field_name] = env_val

        known = {f.name for f in fields(cls)}
        return cls(**{k: str(v) for k, v in data.items() if k in known})
