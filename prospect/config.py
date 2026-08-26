"""Config loading and versioning.

Every derived value the pipeline writes carries the config version that produced
it, so a firm's score on any past date can be recomputed exactly. The declared
config_version in sources.yml is the human facing number; the fingerprint is the
content hash, which catches edits somebody forgot to bump the version for.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

# Data and database locations are overridable so a container can keep them on a
# mounted volume while the code stays read-only. Defaults are unchanged, so a
# local checkout behaves exactly as before with no environment set.
DATA_DIR = Path(os.environ.get("BELLWETHER_DATA") or ROOT / "data")
SNAPSHOT_DIR = DATA_DIR / "snapshots"
DB_PATH = Path(os.environ.get("BELLWETHER_DB") or ROOT / "prospect.db")

# The accounts file is the one piece of config that is written at runtime rather
# than edited by hand, so it is the one that cannot live inside the image: a
# container's filesystem is rebuilt on every deploy, and accounts created in it
# would disappear with it. Overridable for that reason alone; unset, it stays in
# config/ exactly as before.
USERS_FILE = Path(os.environ.get("BELLWETHER_USERS") or CONFIG_DIR / "users.yml")


class ConfigError(RuntimeError):
    """Raised when config is missing or internally inconsistent."""


class Config:
    def __init__(self, path: Path):
        self.path = path
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        raw = path.read_bytes()
        self.fingerprint = hashlib.sha256(raw).hexdigest()[:16]
        self.data: dict[str, Any] = yaml.safe_load(raw.decode("utf-8"))
        if "config_version" not in self.data:
            raise ConfigError(f"{path.name} has no config_version")
        self.version = int(self.data["config_version"])

    @property
    def stamp(self) -> str:
        """Value stored alongside derived data. Version plus content hash."""
        return f"v{self.version}+{self.fingerprint}"

    def source(self, key: str) -> dict[str, Any]:
        try:
            return self.data["sources"][key]
        except KeyError as exc:
            raise ConfigError(f"no source '{key}' in {self.path.name}") from exc

    @property
    def http(self) -> dict[str, Any]:
        """HTTP settings, with the contact address overridable by environment.

        The SEC requires a real contact in the User-Agent, which means the
        config file would otherwise carry a personal email address into a shared
        repository. BELLWETHER_CONTACT lets each deployment supply its own
        without editing tracked config, and changing it must not change the
        config fingerprint, since the contact address has no bearing on how any
        score was computed."""
        h = dict(self.data["http"])
        contact = os.environ.get("BELLWETHER_CONTACT", "").strip()
        if contact:
            h["user_agent"] = f"Acumen Strategy Research {contact}"
        return h


def load(name: str = "sources.yml") -> Config:
    return Config(CONFIG_DIR / name)


def ensure_dirs() -> None:
    for d in (DATA_DIR, SNAPSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
