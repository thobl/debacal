"""Bounded, deterministic date patterns for irregular series.

Every weekday token consumes one week. ``X`` consumes an empty week, and
``n(...)`` repeats a sequence. Thus ``3(Mo X) 2X 2(Mo X)`` creates three
fortnightly Mondays, two additional empty weeks, then two fortnightly Mondays.
The first weekday is aligned on or after ``anchor``.

Comma-separated dates support DD.MM., DD.MM.YYYY and ISO YYYY-MM-DD.
Missing years roll forward in list order; explicit years are never changed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import re


class PatternError(ValueError):
    """A pattern is invalid or would exceed a documented expansion bound."""


def shift_expression_anchor(anchor: str, days: int) -> str:
    """Move the authoring anchor with a whole-series wall-clock date shift.

    The expression itself is deliberately untouched, retaining exact whitespace
    and language. A time-only move leaves its anchor date unchanged.
    """
    if not anchor:
        return ''
    try:
        return (date.fromisoformat(anchor) + timedelta(days=days)).isoformat()
    except (ValueError, OverflowError) as exc:
        raise PatternError('The custom schedule anchor cannot move outside the supported date range.') from exc


def reconstruct_pattern(dates: list[date]) -> str | None:
    """Build equivalent weekly language for a legacy same-weekday date set.

    This is a reconstruction, not a claim about the original expression. The
    smallest gap determines the base cadence; larger gaps become extra skips.
    Arbitrary date sets remain best represented as explicit dates.
    """
    if len(dates) < 2 or any(not isinstance(day, date) or isinstance(day, datetime) for day in dates):
        return None
    ordered = sorted(set(dates))
    if len(ordered) < 2 or len({day.weekday() for day in ordered}) != 1:
        return None
    gaps = [(later - earlier).days // 7 for earlier, later in zip(ordered, ordered[1:])]
    cadence = min(gaps)
    if cadence < 1:
        return None
    weekday = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'][ordered[0].weekday()]
    skipped = cadence - 1
    block = weekday + (' X' if skipped == 1 else f' {skipped}X' if skipped else '')
    parts = []
    run = 1
    for gap in gaps:
        if gap == cadence:
            run += 1
        else:
            parts.extend((f'{run}({block})', f'{gap - cadence}X'))
            run = 1
    parts.append(f'{run}({block})')
    expression = ' '.join(parts)
    # The expression editor has bounded expansion; retain a date-list fallback
    # instead of producing a string that its own parser cannot open.
    if len(expression) > MAX_PATTERN_LENGTH or (ordered[-1] - ordered[0]).days // 7 + cadence > MAX_WEEKS:
        return None
    return expression


def shift_schedule_expression(expression: str, anchor: str, days: int,
                              dates: list[date]) -> tuple[str, str]:
    """Reflect an intentional day movement in editable authoring language.

    Single-weekday patterns retain spacing and repetition syntax. Date lists
    and more complex patterns use an equivalent expression for the moved
    functional dates, avoiding a hidden offset that would surprise the editor.
    Time-only edits preserve the literal text byte for byte.
    """
    if not expression or not days:
        return expression, anchor
    moved_anchor = shift_expression_anchor(anchor, days)
    words = re.findall(r'[A-Za-z]+', expression)
    weekdays = { _DAYS[word.lower()] for word in words if word.lower() in _DAYS }
    if len(weekdays) == 1 and all(word.lower() in _DAYS or word.lower() == 'x' for word in words):
        if days % 7 == 0:
            return expression, moved_anchor
        def rotate(match):
            token = match[0]
            value = token.lower()
            if value not in _DAYS:
                return token
            if value in {'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'}:
                names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            elif value in {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'}:
                names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
            elif value in {'di', 'mi', 'do', 'so'}:
                names = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']
            else:
                names = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su']
            name = names[(_DAYS[value] + days) % 7]
            return name.upper() if token.isupper() else name.lower() if token.islower() else name
        return re.sub(r'[A-Za-z]+', rotate, expression), moved_anchor
    ordered = sorted(set(dates))
    replacement = reconstruct_pattern(ordered) or ', '.join(day.isoformat() for day in ordered)
    return replacement, ordered[0].isoformat() if ordered else moved_anchor


MAX_PATTERN_LENGTH = 8192
MAX_WEEKS = 5200
MAX_NESTING = 8
_DAYS = {
    "mo": 0, "mon": 0, "monday": 0,
    "tu": 1, "tue": 1, "tuesday": 1, "di": 1,
    "we": 2, "wed": 2, "wednesday": 2, "mi": 2,
    "th": 3, "thu": 3, "thursday": 3, "do": 3,
    "fr": 4, "fri": 4, "friday": 4,
    "sa": 5, "sat": 5, "saturday": 5,
    "su": 6, "sun": 6, "sunday": 6, "so": 6,
}
_TOKEN = re.compile(r"\d+|[A-Za-z]+|[()]")
_DMY = re.compile(r"(\d{1,2})\.(\d{1,2})\.(?:(\d{4})\.?)?")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
        raise PatternError("The event limit must be an integer between 1 and 10000.")


def _date_list(pattern: str, anchor: date, limit: int) -> list[date]:
    parts = pattern.split(",")
    if len(parts) > limit:
        raise PatternError(f"This pattern exceeds the limit of {limit} events.")
    result = []
    cursor = anchor
    for index, part in enumerate(parts, start=1):
        part = part.strip()
        if not part:
            raise PatternError(f"Date {index} is empty. Remove the extra comma.")
        try:
            if _ISO.fullmatch(part):
                value = date.fromisoformat(part)
            elif match := _DMY.fullmatch(part):
                day, month = int(match[1]), int(match[2])
                if match[3]:
                    value = date(int(match[3]), month, day)
                else:
                    # Validate against a leap year before searching for the next
                    # actual occurrence, so e.g. 31 April fails immediately.
                    date(2000, month, day)
                    value = None
                    for year in range(cursor.year, min(cursor.year + 9, 10000)):
                        try:
                            candidate = date(year, month, day)
                        except ValueError:
                            continue
                        if candidate >= cursor:
                            value = candidate
                            break
                    if value is None:
                        raise ValueError("date is outside the supported year range")
            else:
                raise PatternError(
                    f"Date {index} ({part!r}) must use DD.MM., DD.MM.YYYY or YYYY-MM-DD."
                )
        except ValueError as exc:
            if isinstance(exc, PatternError):
                raise
            raise PatternError(f"Date {index} ({part!r}) is invalid: {exc}.") from exc
        if value < anchor:
            raise PatternError(f"Date {index} ({part!r}) is before the start date {anchor.isoformat()}.")
        result.append(value)
        cursor = max(cursor, value)
    return sorted(set(result))


class _Parser:
    def __init__(self, pattern: str, limit: int):
        self.tokens: list[tuple[str, int]] = []
        self.limit = limit
        self.position = 0
        cursor = 0
        for match in _TOKEN.finditer(pattern):
            if pattern[cursor:match.start()].strip():
                raise PatternError(f"Unexpected character at position {cursor + 1}.")
            self.tokens.append((match[0], match.start()))
            cursor = match.end()
        if pattern[cursor:].strip():
            raise PatternError(f"Unexpected character at position {cursor + 1}.")

    def _bounded(self, values: list[int | None]) -> list[int | None]:
        if len(values) > MAX_WEEKS:
            raise PatternError(f"A pattern may span at most {MAX_WEEKS} weeks.")
        if sum(day is not None for day in values) > self.limit:
            raise PatternError(f"This pattern exceeds the limit of {self.limit} events.")
        return values

    def sequence(self, depth: int = 0) -> list[int | None]:
        if depth > MAX_NESTING:
            raise PatternError(f"Patterns may nest at most {MAX_NESTING} levels.")
        values: list[int | None] = []
        while self.position < len(self.tokens):
            token, offset = self.tokens[self.position]
            if token == ")":
                if depth == 0:
                    raise PatternError(f"Unexpected closing parenthesis at position {offset + 1}.")
                break
            count = 1
            if token.isdigit():
                if len(token) > 6 or not 1 <= int(token) <= MAX_WEEKS:
                    raise PatternError(f"A repeat count must be between 1 and {MAX_WEEKS}.")
                count = int(token)
                self.position += 1
                if self.position == len(self.tokens):
                    raise PatternError("A repeat count must be followed by (...) or X.")
                token, offset = self.tokens[self.position]
                if token.lower() != "x" and token != "(":
                    raise PatternError("Repeat a weekday using e.g. 3(Mo), or skip weeks using 3X.")
            self.position += 1
            if token == "(":
                unit = self.sequence(depth + 1)
                if self.position == len(self.tokens):
                    raise PatternError(f"Missing closing parenthesis for position {offset + 1}.")
                self.position += 1
                if not unit:
                    raise PatternError("An empty (...) group is not allowed.")
            elif token.lower() == "x":
                unit = [None]
            elif token.lower() in _DAYS:
                unit = [_DAYS[token.lower()]]
            else:
                raise PatternError(
                    f"Unknown weekday {token!r}. Use Mo Tu We Th Fr Sa Su, or X to skip a week."
                )
            # Check before multiplying so hostile nested repeats cannot allocate
            # a large intermediate list.
            if len(values) + len(unit) * count > MAX_WEEKS:
                raise PatternError(f"A pattern may span at most {MAX_WEEKS} weeks.")
            if sum(day is not None for day in values) + sum(day is not None for day in unit) * count > self.limit:
                raise PatternError(f"This pattern exceeds the limit of {self.limit} events.")
            values.extend(unit * count)
        return self._bounded(values)


def expand_pattern(pattern: str, anchor: date, limit: int = 500) -> list[date]:
    """Return sorted unique dates on/after anchor, or raise :class:`PatternError`.

    A blank pattern means a single event on ``anchor``. Patterns are limited to
    8192 characters, eight nesting levels, 5200 weekly slots, and ``limit`` dates.
    """
    _validate_limit(limit)
    if not isinstance(pattern, str):
        raise PatternError("A date pattern must be text.")
    if not isinstance(anchor, date):
        raise PatternError("A start date is required.")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise PatternError(f"A pattern may contain at most {MAX_PATTERN_LENGTH} characters.")
    pattern = pattern.strip()
    if not pattern:
        return [anchor]
    if "." in pattern or "," in pattern or _ISO.fullmatch(pattern):
        return _date_list(pattern, anchor, limit)
    slots = _Parser(pattern, limit).sequence()
    if all(day is None for day in slots):
        raise PatternError("The pattern only skips weeks. Include at least one weekday.")
    first_day = next(day for day in slots if day is not None)
    week = anchor - timedelta(days=anchor.weekday())
    # Align the first named weekday, retaining any leading X slots.
    if first_day < anchor.weekday():
        try:
            week += timedelta(weeks=1)
        except OverflowError as exc:
            raise PatternError("The pattern extends beyond the supported date range.") from exc
    try:
        return [week + timedelta(weeks=index, days=day)
                for index, day in enumerate(slots) if day is not None]
    except OverflowError as exc:
        raise PatternError("The pattern extends beyond the supported date range.") from exc


def describe_pattern(pattern: str, anchor: date, limit: int = 500) -> str:
    """A concise preview summary; invalid input raises ``PatternError``."""
    dates = expand_pattern(pattern, anchor, limit)
    if len(dates) == 1:
        return f"1 event · {dates[0]:%a %d %b %Y}"
    return f"{len(dates)} events · {dates[0]:%a %d %b %Y} – {dates[-1]:%a %d %b %Y}"
