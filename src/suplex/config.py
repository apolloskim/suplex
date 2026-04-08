"""Configuration — reads from ~/.config/suplex/config.toml, env vars as fallback."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
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


@dataclass
class Config:
    apify_token: str = ""
    anthropic_api_key: str = ""
    reacher_api_key: str = ""
    reacher_url: str = ""
    million_verifier_api_key: str = ""

    def is_configured(self, key: str) -> bool:
        return bool(getattr(self, key, ""))

    def missing_keys(self) -> list[str]:
        # Only required keys.
        required = ["apify_token", "anthropic_api_key"]
        return [k for k in required if not self.is_configured(k)]

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Suplex configuration\n"]
        for f in fields(self):
            val = getattr(self, f.name)
            if val:
                lines.append(f'{f.name} = "{val}"')
        CONFIG_PATH.write_text("\n".join(lines) + "\n")

    @classmethod
    def load(cls) -> Config:
        """Load config from file, then overlay env vars."""
        data: dict[str, str] = {}

        # Read from file if exists.
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)

        # Env vars override file values.
        for field_name, env_name in ENV_MAP.items():
            env_val = os.environ.get(env_name)
            if env_val:
                data[field_name] = env_val

        return cls(**{k: v for k, v in data.items() if k in {f.name for f in fields(cls)}})
