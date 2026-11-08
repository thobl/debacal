"""Both year appearances share calendar summaries and native navigation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.year_view import YearView


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
DAY = START.date()


@pytest.fixture(params=['tracks', 'tints'])
def year(request):
    app = QApplication.instance() or QApplication([])
    with patch('debacal.year_view.local_zone', return_value=ZONE), \
         patch('debacal.year_view.now', return_value=START):
        view = YearView()
        view.set_preferences(Preferences(locale_name='en_GB', year_view_style=request.param))
        view.resize(1040, view.heightForWidth(1040))
        view.show()
        app.processEvents()
        yield view
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def appointment(identity, calendar_id, **changes):
    return replace(Event(identity, calendar_id, 'Appointment', START, START + timedelta(hours=1)), **changes)


def test_calendar_presence_uses_source_order_and_filters_hidden_and_disabled_sources(year):
    calendars = [Calendar('z', 'First', '#547797'), Calendar('a', 'Second', '#557e69'),
                 Calendar('hidden', 'Hidden', '#ef2639', visible=False),
                 Calendar('disabled', 'Disabled', '#d17527', enabled=False),
                 Calendar('read-only', 'Read only', '#9b85ad', read_only=True)]
    events = [appointment('first', 'a'), appointment('duplicate', 'a'),
              appointment('next', 'z'), appointment('hidden', 'hidden'),
              appointment('disabled', 'disabled'), appointment('read-only', 'read-only'),
              appointment('unknown-z', 'unknown-z'), appointment('unknown-b', 'unknown-b')]
    year.set_data(2026, events, calendars)
    assert year.calendars_on_day(DAY) == ('z', 'a', 'read-only', 'unknown-b', 'unknown-z')
    assert {item.id for item in year.events_on_day(DAY)} == {
        'first', 'duplicate', 'next', 'read-only', 'unknown-z', 'unknown-b'}
    assert year._summaries[(DAY - date(2026, 1, 1)).days]['a'][0] == 2
    assert year.calendars_on_day(date(2025, 12, 31)) == ()
    assert year.calendars_on_day(date(2027, 1, 1)) == ()
    year._ensure_geometry()
    assert not year.hits and not year._marker_hits
    # Showing a previously hidden calendar adds it at its source-list position.
    year.set_data(2026, events, [replace(item, visible=True) for item in calendars])
    assert year.calendars_on_day(DAY) == ('z', 'a', 'hidden', 'read-only', 'unknown-b', 'unknown-z')


def test_multiday_calendar_presence_and_counts_respect_exclusive_end_dates(year):
    calendars = [Calendar('work', 'Work', '#547797'), Calendar('personal', 'Personal', '#557e69')]
    events = [appointment('workshop', 'work', start=START.replace(hour=0),
                          end=START.replace(hour=0) + timedelta(days=3), all_day=True),
              appointment('late', 'personal', start=START.replace(hour=23),
                          end=START.replace(hour=0) + timedelta(days=1))]
    year.set_data(2026, events, calendars)
    assert year.calendars_on_day(DAY) == ('work', 'personal')
    assert year.calendars_on_day(DAY + timedelta(days=1)) == ('work',)
    assert year.calendars_on_day(DAY + timedelta(days=2)) == ('work',)
    assert year.calendars_on_day(DAY + timedelta(days=3)) == ()


def test_dense_day_tooltip_includes_calendars_beyond_the_visible_indicators(year):
    calendars = [Calendar(str(index), f'Calendar {index}', '#547797') for index in range(8)]
    events = [appointment(str(index), str(index)) for index in range(8)]
    year.set_data(2026, events, calendars)
    QTest.mouseMove(year, QPoint(1, 1))
    QTest.mouseMove(year, year.day_rect(DAY).center().toPoint())
    assert len(year.calendars_on_day(DAY)) == 8
    assert all(source.name in year.toolTip() for source in calendars)
    assert not year.grab().isNull()


def test_narrow_tall_day_keeps_visible_calendar_colors_beside_overflow(year):
    from PySide6.QtGui import QColor
    colors = ['#c43250', '#3279c4', '#36a569', '#bc8e21', '#974db6', '#459eaa']
    calendars = [Calendar(str(index), f'Calendar {index}', color) for index, color in enumerate(colors)]
    year.set_data(2026, [appointment(str(index), str(index)) for index in range(len(colors))], calendars)
    year.resize(400, 2400)
    image = year.grab(year.day_rect(DAY).toAlignedRect()).toImage()
    painted = {image.pixelColor(x, y).rgba() for y in range(image.height()) for x in range(image.width())}
    assert all(QColor(color).rgba() in painted for color in colors[:3])


def test_both_appearances_share_day_month_week_and_creation_gestures(year):
    year.set_data(2026, [appointment('one', 'local')], [Calendar('local', 'Local', '#547797')])
    days, months, weeks, created, selected = [], [], [], [], []
    year.day_selected.connect(days.append)
    year.month_requested.connect(months.append)
    year.week_requested.connect(weeks.append)
    year.create_all_day_requested.connect(lambda first, last: created.append((first, last)))
    year.selected.connect(selected.append)
    QTest.mouseClick(year, Qt.MouseButton.LeftButton, pos=year.day_rect(DAY).center().toPoint())
    QTest.mouseClick(year, Qt.MouseButton.LeftButton, pos=year.month_rect(11).center().toPoint())
    QTest.mouseClick(year, Qt.MouseButton.LeftButton, pos=year.week_rect(11, 1).center().toPoint())
    QTest.mouseDClick(year, Qt.MouseButton.LeftButton, pos=year.day_rect(DAY).center().toPoint())
    assert days == [DAY, DAY] and not selected
    assert months == [date(2026, 11, 1)]
    assert weeks == [date(2026, 11, 2)]
    assert created == [(START.replace(hour=0), START.replace(hour=0) + timedelta(days=1))]


@pytest.mark.parametrize('mode', ['light', 'dark'])
@pytest.mark.parametrize('font_scale', [100, 140])
def test_changing_appearance_preserves_data_geometry_and_selection_without_reindexing(year, mode, font_scale):
    source = Calendar('work', 'Work', '#547797')
    year.set_data(2026, [appointment('one', source.id)], [source])
    year.set_selection({'one'})
    year.set_selected_day(DAY)
    year.set_preferences(replace(year.preferences, appearance_mode=mode, font_scale=font_scale,
                                 year_view_style='tracks'))
    year.resize(1040, year.heightForWidth(1040))
    before_day, before_month = year.day_rect(DAY), year.month_rect(11)
    before = year.grab().toImage()
    with patch.object(year, '_summarize', side_effect=AssertionError('Appearance must reuse summaries')):
        year.set_preferences(replace(year.preferences, year_view_style='tints'))
        assert year.day_rect(DAY) == before_day and year.month_rect(11) == before_month
        after = year.grab().toImage()
    assert before != after
    assert year.calendars_on_day(DAY) == ('work',)
    assert year.selected_ids == {'one'} and year.selected_day == DAY
    assert not year.hits and not year._marker_hits
