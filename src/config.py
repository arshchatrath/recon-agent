"""Loads config.yaml, and .env if present."""
import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"


def load_env(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env into the environment.

    A real environment variable always wins, so `export GEMINI_API_KEY=...`
    overrides the file rather than the other way round. Deliberately not
    python-dotenv: this is eight lines and one fewer dependency.
    """
    f = Path(path or ENV_PATH)
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@lru_cache(maxsize=1)
def load(path: str | None = None) -> dict:
    load_env()
    return yaml.safe_load(Path(path or CONFIG_PATH).read_text())
