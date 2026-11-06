"""Calendar imports may refer to a file or already received calendar bytes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MAX_IMPORT_BYTES = 20_000_000
MAX_PENDING_IMPORT_BYTES = 3 * MAX_IMPORT_BYTES
MAX_PENDING_IMPORT_REQUESTS = 128


@dataclass(frozen=True, slots=True)
class ImportSource:
    path: Path | None = None
    data: bytes | None = None
    label: str = ''

    def __post_init__(self):
        if (self.path is None) == (self.data is None):
            raise ValueError('Choose a calendar file or provide calendar contents.')
        if self.path is not None and not isinstance(self.path, Path):
            raise ValueError('A calendar file needs a local path.')
        if self.data is not None:
            if not isinstance(self.data, bytes):
                raise ValueError('Calendar contents must be provided as bytes.')
            if not self.data:
                raise ValueError('The supplied calendar is empty.')
            if len(self.data) > MAX_IMPORT_BYTES:
                raise ValueError('Choose calendar contents smaller than 20 MB.')
        if not isinstance(self.label, str) or not self.label or '\0' in self.label:
            raise ValueError('An import source needs a name.')

    @classmethod
    def from_path(cls, path: str | Path) -> ImportSource:
        # Resolving links or checking availability may block on remote mounts.
        # Files are only inspected later by the background import worker.
        path = Path(path).expanduser().absolute()
        return cls(path=path, label=path.name)

    @classmethod
    def from_data(cls, data: bytes, label: str = 'Standard input') -> ImportSource:
        return cls(data=data, label=label)
