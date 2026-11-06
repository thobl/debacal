"""Persistent workspaces with separate calendar data and account credentials."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from PySide6.QtCore import QStandardPaths


def validate_profile_name(value: str) -> str:
    """Return a canonical profile name that cannot escape the profile directory."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("Use a profile name of 1–64 letters, numbers, hyphens, or underscores, starting with a letter or number.")
    return value.lower()


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    database: Path
    credential_namespace: str

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").replace("-", " ").title()


def resolve_profile(name: str = "default", database: str | Path | None = None, *,
                    data_root: str | Path | None = None) -> Profile:
    """Resolve a workspace without opening, copying, or creating any files.

    The regular profile keeps the existing database and keyring service.
    Named profiles and explicit alternative databases have isolated credentials,
    even if they connect to the same server and login as the regular workspace.
    """
    name = validate_profile_name(name)
    if data_root is None:
        data_root = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if not data_root:
            raise ValueError("The operating system did not provide an application data directory.")
    root = Path(data_root).expanduser().resolve()
    normal = root / "calendar.sqlite3"
    selected = (Path(database).expanduser() if database is not None else
                normal if name == "default" else root / "profiles" / name / "calendar.sqlite3")
    selected = selected.resolve()
    namespace = ""
    if name != "default" or selected != normal.resolve():
        digest = hashlib.sha256(str(selected).encode()).hexdigest()
        namespace = f"profile:{name}:database:{digest}"
    return Profile(name, selected, namespace)
