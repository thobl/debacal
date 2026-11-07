"""Persisted upcoming layouts, local day grouping, native input and search parity."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from debacal.agenda_style import AgendaPresentation
from debacal.appearance import stylesheet
from debacal.models import Event
from debacal.preferences import Preferences, SETTINGS_KEY
from debacal.preferences_dialog import PreferencesDialog
from debacal.search_panel import SearchPanel, EVENT_ROLE
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
NOW = datetime(2026, 11, 6, 13, 15, tzinfo=ZONE)


def event(identity, start, *, end=None, **values):
    return Event(identity, 'local', 'Synthetic needle ' + identity, start,
                 end or start + timedelta(hours=1), **values)


def settle(app):
    for _ in range(6):
        app.processEvents()


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('debacal.search_panel.now', return_value=NOW), \
         patch('debacal.search_panel.local_zone', return_value=ZONE), \
         patch('debacal.ui.now', return_value=NOW), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        yield app, widgets
        for widget in widgets:
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


@pytest.mark.parametrize('style', ['cards', 'grouped', 'timeline'])
def test_agenda_layout_roundtrips_and_keeps_older_positional_preferences(tmp_path, style):
    path = tmp_path / 'preferences.sqlite3'
    with Store(path) as store:
        Preferences(agenda_style=style).save(store)
    with Store(path) as store:
        assert Preferences.load(store).agenda_style == style
    older = Preferences(6, 'iso', '12h', 'both', False, 15, 'de_DE', 120, True, 6, 22, 'warm')
    assert older.agenda_style == 'timeline'


@pytest.mark.parametrize('invalid', [None, 'unknown', [], {}, True, 1])
def test_missing_or_invalid_saved_layout_uses_timeline(invalid):
    with Store(':memory:') as store:
        store.set_setting(SETTINGS_KEY, {'agenda_style': invalid, 'date_format': 'iso'})
        assert Preferences.load(store) == Preferences(date_format='iso')
        store.set_setting(SETTINGS_KEY, {'date_format': 'iso'})
        assert Preferences.load(store).agenda_style == 'timeline'
    with pytest.raises(ValueError):
        Preferences(agenda_style=invalid)


def test_appearance_selector_saves_and_restores_the_default(desktop):
    _, widgets = desktop
    dialog = PreferencesDialog(Preferences(agenda_style='cards'))
    widgets.append(dialog)
    dialog.set_category('Appearance')
    assert dialog.agenda_style.currentData() == 'cards'
    dialog.agenda_style.setCurrentIndex(dialog.agenda_style.findData('grouped'))
    dialog.save()
    assert dialog.result_preferences.agenda_style == 'grouped'
    dialog.restore_defaults()
    assert dialog.value().agenda_style == 'timeline'


def examples():
    midnight = NOW.replace(day=5, hour=0, minute=0)
    return [event('break', midnight, end=midnight + timedelta(days=3), all_day=True),
            event('today-a', NOW.replace(hour=14)), event('today-b', NOW.replace(hour=18)),
            event('tomorrow', NOW.replace(day=7, hour=9))]


def test_day_groups_keep_ongoing_spans_and_stop_rails_at_each_day():
    presentation = AgendaPresentation()
    preferences = Preferences(agenda_style='timeline', date_format='iso', time_format='24h', locale_name='en_GB')
    assert presentation.update(examples(), preferences, NOW, ZONE)
    ongoing, first, last, tomorrow = (presentation.rows[name]
                                        for name in ('break', 'today-a', 'today-b', 'tomorrow'))
    assert ongoing.group == 'ongoing' and ongoing.heading == 'Ongoing'
    assert ongoing.first and ongoing.last and ongoing.detail == 'Until Sat, 2026-11-07'
    assert first.heading.startswith('Today') and first.first and not first.last
    assert last.heading == '' and not last.first and last.last
    assert tomorrow.heading.startswith('Tomorrow') and tomorrow.first and tomorrow.last
    assert (ongoing.body_height, first.body_height) == (49, 40)


def test_grouping_uses_local_dates_across_dst_and_exclusive_all_day_ends():
    current = datetime(2026, 10, 25, 1, 30, tzinfo=ZONE)
    first = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1)
    tomorrow = datetime(2026, 10, 26, 0, 15, tzinfo=ZONE)
    midnight = datetime(2026, 10, 24, tzinfo=ZONE)
    values = [event('span', midnight, end=datetime(2026, 10, 26, tzinfo=ZONE), all_day=True),
              event('first', first.astimezone(timezone.utc),
                    end=first.astimezone(timezone.utc) + timedelta(minutes=15)),
              event('second', second.astimezone(timezone.utc),
                    end=second.astimezone(timezone.utc) + timedelta(minutes=15)),
              event('tomorrow', tomorrow.astimezone(timezone.utc))]
    preferences = Preferences(agenda_style='timeline', date_format='iso', time_format='24h', locale_name='en_GB')
    presentation = AgendaPresentation()
    presentation.update(values, preferences, current, ZONE)
    assert presentation.rows['span'].detail == 'Until Sun, 2026-10-25'
    assert presentation.rows['first'].clock == presentation.rows['second'].clock == '02:30'
    assert presentation.rows['first'].heading.startswith('Today')
    assert presentation.rows['second'].heading == ''
    assert presentation.rows['tomorrow'].heading.startswith('Tomorrow')


def make_panel(desktop, store, style='timeline'):
    app, widgets = desktop
    preferences = Preferences(agenda_style=style, date_format='iso', time_format='24h', locale_name='en_GB')
    panel = SearchPanel(store, preferences)
    widgets.append(panel)
    panel.setStyleSheet(stylesheet())
    panel.resize(320, 400)
    panel.set_range(NOW, NOW + timedelta(days=7))
    panel.show()
    settle(app)
    return panel


def test_style_changes_relayout_existing_rows_without_losing_selection(desktop):
    app, _ = desktop
    with Store(':memory:') as store:
        store.apply('Create', examples())
        panel = make_panel(desktop, store, 'cards')
        panel.populate_upcoming(NOW)
        item = panel.topLevelItem(1)
        panel.setCurrentItem(item)
        settle(app)
        assert panel.uniformRowHeights() and not panel.rootIsDecorated()
        assert panel.visualItemRect(item).height() == 49
        for style, height in [('grouped', 69), ('timeline', 60), ('cards', 49)]:
            panel.update_context(store.list_calendars(), replace(panel.preferences, agenda_style=style))
            settle(app)
            assert panel.topLevelItem(1) is item and panel.currentItem() is item
            assert panel.visualItemRect(item).height() == height
            assert panel.uniformRowHeights() == (style == 'cards')


def test_midnight_refresh_updates_day_labels_without_querying_or_replacing_rows(desktop):
    _, _widgets = desktop
    before = NOW.replace(hour=23, minute=59)
    after = before + timedelta(minutes=2)
    with Store(':memory:') as store:
        store.apply('Create', [event('tomorrow', NOW.replace(day=7, hour=9))])
        panel = make_panel(desktop, store)
        panel.populate_upcoming(before)
        item = panel.topLevelItem(0)
        panel.setCurrentItem(item)
        delegate = panel.itemDelegate()
        assert delegate.row_layout(panel.indexFromItem(item)).heading.startswith('Tomorrow')
        with patch.object(store, 'event_search_index', side_effect=AssertionError('No reindex')), \
             patch.object(store, 'upcoming_occurrences', side_effect=AssertionError('No query')):
            assert panel.refresh_agenda_presentation(after)
            assert not panel.refresh_agenda_presentation(after + timedelta(seconds=10))
        assert delegate.row_layout(panel.indexFromItem(item)).heading.startswith('Today')
        assert panel.topLevelItem(0) is item and panel.currentItem() is item


def test_painting_reuses_preformatted_agenda_rows(desktop):
    app, _ = desktop
    with Store(':memory:') as store:
        store.apply('Create', examples())
        panel = make_panel(desktop, store)
        panel.populate_upcoming(NOW)
        settle(app)
        with patch.object(Preferences, 'format_date', side_effect=AssertionError('Dates are cached')), \
             patch.object(Preferences, 'format_time', side_effect=AssertionError('Times are cached')):
            panel.viewport().repaint()
            assert not panel.viewport().grab().isNull()


@pytest.mark.parametrize('scale', [80, 100, 140])
@pytest.mark.parametrize('time_format', ['24h', '12h'])
def test_timeline_column_fits_all_day_and_formatted_times_with_a_shared_rail(desktop, scale, time_format):
    app, _ = desktop
    with Store(':memory:') as store:
        store.apply('Create', examples())
        panel = make_panel(desktop, store)
        preferences = replace(panel.preferences, font_scale=scale, time_format=time_format)
        panel.setStyleSheet(stylesheet(scale))
        panel.update_context(store.list_calendars(), preferences)
        panel.populate_upcoming(NOW)
        settle(app)
        delegate = panel.itemDelegate()
        rails = set()
        for i in range(panel.topLevelItemCount()):
            item = panel.topLevelItem(i)
            index = panel.indexFromItem(item)
            row = delegate.row_layout(index)
            option = QStyleOptionViewItem()
            option.initFrom(panel)
            option.widget, option.font, option.rect = panel, panel.font(), panel.visualItemRect(item)
            body, _card, rail = delegate.geometry(option, row)
            factor = scale / 100
            available = rail - body.left() - 6 * factor
            width = QFontMetricsF(delegate._small_font(option.font, factor)).horizontalAdvance(row.clock)
            assert available >= width + factor
            rails.add(rail - body.left())
        assert len(rails) == 1


def test_all_agenda_styles_return_to_identical_search_cards_and_native_folding(desktop):
    app, _ = desktop
    images = []
    with Store(':memory:') as store:
        store.apply('Create', examples() + [event('series', NOW.replace(hour=15), recurrence_rule='FREQ=DAILY;COUNT=3')])
        for style in ('cards', 'grouped', 'timeline'):
            panel = make_panel(desktop, store, style)
            panel.populate_upcoming(NOW)
            panel.populate('needle')
            series = next(panel.topLevelItem(i) for i in range(panel.topLevelItemCount())
                          if panel.event_id(panel.topLevelItem(i)) == 'series')
            rect = panel.visualItemRect(series)
            QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton,
                             pos=QPoint(rect.left() - panel.indentation() // 2, rect.center().y()))
            settle(app)
            assert series.isExpanded() and series.childCount() == 3
            panel.setCurrentItem(series.child(0))
            panel.clearFocus()
            settle(app)
            assert panel.uniformRowHeights() and panel.rootIsDecorated()
            assert panel.visualItemRect(series).height() == 49
            images.append(panel.viewport().grab().toImage())
        assert images[0] == images[1] == images[2]


def test_timeline_clock_selection_delete_and_undo_keep_original_event_identity(desktop):
    app, widgets = desktop
    with Store(':memory:') as store:
        value = event('entry', NOW.replace(hour=15))
        store.apply('Create', [value])
        window = MainWindow(store, enable_sync=False)
        widgets.append(window)
        window.go_date(NOW.date())
        window.show()
        window.activateWindow()
        settle(app)
        item = window.results.topLevelItem(0)
        assert item.data(EVENT_ROLE).id == value.id
        rect = window.results.visualItemRect(item)
        QTest.mouseClick(window.results.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(rect.left() + 10, rect.bottom() - 15))
        settle(app)
        assert window.selected_id == value.id
        QTest.keyClick(window.results, Qt.Key.Key_Delete)
        settle(app)
        assert store.get_event(value.id) is None
        window.undo()
        assert store.get_event(value.id) == value
