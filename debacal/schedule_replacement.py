"""Distinguish an edited schedule from an explicitly chosen clean pattern."""
from enum import Enum


class ScheduleReplacement(Enum):
    # The edited canonical fields, including an empty exception set, are final.
    # Boolean True retains the existing merge behavior for graphical edits.
    RESET = 'reset'
