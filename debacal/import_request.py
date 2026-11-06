"""An import delivered by a second command without temporary calendar files."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImportRequest:
    files: tuple[str, ...]
    stdin_data: bytes
