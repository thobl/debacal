"""Bound dateutil work without changing recurrence dates or process globals.

Dateutil checks UNTIL only when it emits a candidate, so a wrapper counting
yielded dates cannot stop empty calendar filters. For complex rules, its small
calendar-helper interface supplies a period checkpoint. Run the existing
iterator code with a private globals dictionary containing a bounded helper
factory; never patch the library, install tracing hooks, or copy its algorithm.
The declared dateutil 2.x dependency is capability-checked before this path.
"""
from types import FunctionType


MAX_PERIOD_WORK = 10000
MAX_CANDIDATE_WORK = 50000
_DAYSETS = {'ydayset', 'mdayset', 'wdayset', 'ddayset'}
_TIMESETS = {'htimeset', 'mtimeset', 'stimeset'}


class _Work:
    def __init__(self, error, limit=None, window_end=None):
        self.error, self.periods, self.clocks = error, 0, 0
        self.window_end = window_end.date() if window_end is not None else None
        self.period_limit = MAX_PERIOD_WORK if limit is None else limit
        self.candidate_limit = MAX_CANDIDATE_WORK if limit is None else limit

    def fail(self):
        raise self.error('This recurrence exceeds the computation budget for this view. '
                         'Inspect dates nearer its start, shorten its recurrence, or use a simpler rule. '
                         'Its saved definition is unchanged.')

    def period(self, year=None, month=None, day=None):
        if self.window_end is not None and (year, month, day) > (
                self.window_end.year, self.window_end.month, self.window_end.day):
            raise _WindowComplete
        self.periods += 1
        if self.periods > self.period_limit:
            self.fail()

    def clock_set(self, size):
        self.clocks += size
        if self.clocks > self.candidate_limit:
            self.fail()


class _CalendarInfo:
    def __init__(self, original, work):
        self.original, self.work = original, work

    def __getattr__(self, name):
        value = getattr(self.original, name)
        if name in _DAYSETS:
            def dayset(*args):
                year, month, day = args
                # A yearly/monthly period may contain selected dates before
                # DTSTART's original month/day in later iterations.
                if name == 'ydayset':
                    month, day = 1, 1
                elif name == 'mdayset':
                    day = 1
                self.work.period(year, month, day)
                return value(*args)
            return dayset
        if name in _TIMESETS:
            def timeset(*args):
                rule = self.original.rrule
                size = (len(rule._byminute) * len(rule._bysecond) if name == 'htimeset'
                        else len(rule._bysecond) if name == 'mtimeset' else 1)
                self.work.clock_set(size)
                return value(*args)
            return timeset
        return value


class BoundedRule:
    def __init__(self, rule, error, *, calendar_periods=False, limit=None, window_end=None):
        self.rule, self.error, self.calendar_periods = rule, error, calendar_periods
        self.limit = limit
        self.window_end = window_end

    def __getattr__(self, name):
        return getattr(self.rule, name)

    def __iter__(self):
        work = _Work(self.error, self.limit, self.window_end)
        if self.calendar_periods or self.window_end is not None:
            iterator = type(self.rule)._iter
            code = getattr(iterator, '__code__', None)
            namespace = getattr(iterator, '__globals__', {})
            factory = namespace.get('_iterinfo')
            if (code is None or '_iterinfo' not in code.co_names or not callable(factory)
                    or any(not callable(getattr(factory, name, None)) for name in _DAYSETS | _TIMESETS)):
                raise self.error('This recurrence engine cannot enforce a safe calendar-work budget. '
                                 'Use a simpler rule or a supported python-dateutil 2.x version. '
                                 'Its saved definition is unchanged.')
            namespace = dict(namespace)
            namespace['_iterinfo'] = lambda rule: _CalendarInfo(factory(rule), work)
            guarded = FunctionType(code, namespace, iterator.__name__, iterator.__defaults__, iterator.__closure__)
            values = guarded(_Rule(self.rule, work))
        else:
            values = iter(self.rule)
        try:
            for index, value in enumerate(values):
                if index >= work.candidate_limit:
                    work.fail()
                yield value
        except _WindowComplete:
            return


class _WindowComplete(Exception):
    """The engine reached the first calendar period beyond a query window."""


class _Times:
    """Count precomputed clock candidates even when BYSETPOS emits none."""
    def __init__(self, values, work):
        self.values, self.work = values, work
        work.clock_set(len(values))

    def __len__(self):
        return len(self.values)

    def __getitem__(self, index):
        self.work.clock_set(1)
        return self.values[index]

    def __iter__(self):
        for value in self.values:
            self.work.clock_set(1)
            yield value


class _Rule:
    def __init__(self, original, work):
        object.__setattr__(self, 'original', original)
        times = getattr(original, '_timeset', None)
        object.__setattr__(self, '_timeset', _Times(times, work) if times is not None else None)

    def __getattr__(self, name):
        return getattr(self.original, name)

    def __setattr__(self, name, value):
        setattr(self.original, name, value)
