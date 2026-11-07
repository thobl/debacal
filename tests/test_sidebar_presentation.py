"""Source controls and event cards keep their distinct roles and native input."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.appearance import stylesheet
from debacal.calendar_list import CalendarRow
from debacal.calendar_widgets import CalendarCanvas
from debacal.event_style import event_colors
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.search_panel import SearchPanel
from debacal.store import Store
from debacal.theme import scheme_for


START = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets = []
    yield app, widgets
    for widget in widgets:
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def settle(app):
    for _ in range(5):
        app.processEvents()


def test_creation_target_has_underlined_text_and_visibility_stays_independent(desktop):
    app, widgets = desktop
    source = Calendar('source', 'Synthetic source', '#b6815c')
    row = CalendarRow(source)
    widgets.append(row)
    row.setStyleSheet(stylesheet())
    row.set_preferences(Preferences())
    row.set_calendar(source, current=True)
    row.resize(290, 40)
    row.show()
    settle(app)
    assert row.choice.isChecked() and row.choice.font().bold() and row.choice.font().underline()
    assert row.swatch.width() == row.swatch.height() == 12
    assert source.color == '#b6815c'
    swatch = row.swatch.grab().toImage()
    assert swatch.pixelColor(6, 6).name() == source.color
    assert swatch.pixelColor(0, 6) != swatch.pixelColor(6, 6)

    creation, visibility = [], []
    row.creation_requested.connect(creation.append)
    row.visibility_requested.connect(lambda *values: visibility.append(values))
    QTest.mouseClick(row.eye, Qt.MouseButton.LeftButton)
    assert visibility == [('source', False)] and not creation
    assert row.choice.isChecked() and row.choice.font().underline()
    QTest.mouseClick(row.choice, Qt.MouseButton.LeftButton)
    assert creation == ['source'] and not row.eye.isChecked()

    row.set_calendar(source, current=False)
    assert not row.choice.font().underline()
    row.set_preferences(Preferences(font_scale=140, color_scheme='warm'))
    assert row.swatch.width() == row.swatch.height() == 17
    row.set_calendar(replace(source, read_only=True), current=True, scale=140)
    assert not row.choice.isEnabled() and not row.choice.isChecked()
    assert not row.choice.font().underline() and row.eye.isEnabled()


def make_panel(desktop, store, preferences):
    app, widgets = desktop
    panel = SearchPanel(store, preferences)
    widgets.append(panel)
    panel.setStyleSheet(stylesheet(preferences.font_scale, preferences.color_scheme))
    panel.set_range(START, START + timedelta(days=7))
    panel.resize(320, 260)
    panel.show()
    settle(app)
    return panel


@pytest.mark.parametrize('scheme', ['neutral', 'warm', 'cool'])
@pytest.mark.parametrize('source', ['#ffff00', '#587c67'])
def test_selected_search_card_keeps_the_shared_source_tint(desktop, scheme, source):
    app, _ = desktop
    with Store(':memory:') as store:
        store.save_calendar(Calendar('source', 'Synthetic source', source))
        event = Event('event', 'source', 'Synthetic needle', START, START + timedelta(hours=1))
        store.apply('Create', [event])
        preferences = Preferences(color_scheme=scheme)
        panel = make_panel(desktop, store, preferences)
        panel.populate('needle')
        panel.clearSelection()
        panel.clearFocus()
        settle(app)
        item = panel.topLevelItem(0)
        rect = panel.visualItemRect(item)
        fill_sample = QPoint(rect.right() - 10, rect.bottom() - 10)
        expected = event_colors(source, scheme)
        plain = panel.viewport().grab().toImage()
        assert plain.pixelColor(fill_sample).name() == expected.fill
        panel.setCurrentItem(item)
        panel.clearFocus()
        settle(app)
        selected = panel.viewport().grab().toImage()
        assert selected.pixelColor(fill_sample).name() == expected.fill
        edge = {selected.pixelColor(x, rect.center().y()).name()
                for x in range(rect.right() - 3, rect.right() + 1)}
        assert expected.selected_outline in edge


def test_card_tree_keeps_native_branch_hits_indentation_and_child_selection(desktop):
    app, _ = desktop
    with Store(':memory:') as store:
        store.apply('Create', [Event('series', 'local', 'Synthetic needle', START,
                                    START + timedelta(hours=1), recurrence_rule='FREQ=DAILY;COUNT=3')])
        panel = make_panel(desktop, store, Preferences())
        panel.populate('needle')
        settle(app)
        root = panel.topLevelItem(0)
        rect = panel.visualItemRect(root)
        QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(rect.left() - panel.indentation() // 2, rect.center().y()))
        settle(app)
        assert root.isExpanded() and root.childCount() == 3
        child = root.child(0)
        child_rect = panel.visualItemRect(child)
        assert child_rect.left() - rect.left() == panel.indentation()
        # The padding around the painted card remains part of the native row hit.
        QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(child_rect.left() + 1, child_rect.bottom()))
        assert panel.selected_row_ids() == {panel.event_id(child)}
        assert not root.isSelected()


def test_selected_event_card_keeps_its_calendar_style_when_focus_moves_to_it(desktop):
    app, _ = desktop
    with Store(':memory:') as store:
        store.save_calendar(Calendar('source', 'Synthetic source', '#7f79a5'))
        store.apply('Create', [Event('event', 'source', 'Synthetic needle', START,
                                    START + timedelta(hours=1))])
        panel = make_panel(desktop, store, Preferences())
        panel.populate('needle')
        item = panel.topLevelItem(0)
        panel.setCurrentItem(item)
        panel.clearFocus()
        settle(app)
        plain = panel.viewport().grab().toImage()
        panel.activateWindow()
        panel.setFocus()
        settle(app)
        assert panel.hasFocus()
        focused = panel.viewport().grab().toImage()
        assert plain == focused


def test_focused_unselected_event_card_keeps_a_keyboard_focus_cue(desktop):
    app, _ = desktop
    with Store(':memory:') as store:
        store.save_calendar(Calendar('source', 'Synthetic source', '#7f79a5'))
        store.apply('Create', [Event('event', 'source', 'Synthetic needle', START,
                                    START + timedelta(hours=1))])
        panel = make_panel(desktop, store, Preferences())
        panel.populate('needle')
        item = panel.topLevelItem(0)
        panel.setCurrentItem(item)
        panel.clearSelection()
        panel.clearFocus()
        settle(app)
        plain = panel.viewport().grab().toImage()
        panel.activateWindow()
        panel.setFocus()
        settle(app)
        assert panel.hasFocus() and not item.isSelected()
        focused = panel.viewport().grab().toImage()
        rect = panel.visualItemRect(item).adjusted(3, 5, -3, -5)
        focus = scheme_for(panel).focus
        ring_pixels = [(x, y) for x in range(rect.left(), rect.right() + 1)
                       for y in range(rect.top(), rect.bottom() + 1)
                       if focused.pixelColor(x, y).name() == focus]
        assert ring_pixels
        assert any(plain.pixelColor(x, y) != focused.pixelColor(x, y) for x, y in ring_pixels)


@pytest.mark.parametrize('mode', ['search', 'cards', 'grouped', 'timeline'])
@pytest.mark.parametrize('scheme', ['neutral', 'warm', 'cool'])
def test_sidebar_selection_matches_the_calendar_source_bar_without_changing_fill(desktop, mode, scheme):
    app, widgets = desktop
    with Store(':memory:') as store:
        source = Calendar('source', 'Synthetic source', '#b6815c')
        entry = Event('event', source.id, 'Synthetic needle', START, START + timedelta(hours=1))
        store.save_calendar(source)
        store.apply('Create', [entry])
        preferences = Preferences(color_scheme=scheme, agenda_style='cards' if mode == 'search' else mode)
        panel = make_panel(desktop, store, preferences)
        if mode == 'search':
            panel.populate('needle')
        else:
            panel.populate_upcoming(START)
        panel.clearSelection()
        panel.clearFocus()
        settle(app)
        item = panel.topLevelItem(0)
        row = panel.visualItemRect(item)
        if mode in ('grouped', 'timeline'):
            from PySide6.QtWidgets import QStyleOptionViewItem
            option = QStyleOptionViewItem()
            option.initFrom(panel)
            option.rect = row
            delegate = panel.itemDelegate()
            _, card, _ = delegate.geometry(option, delegate.row_layout(panel.indexFromItem(item)))
        else:
            card = QRectF(row).adjusted(1, 3, -1, -3)
        bar_sample = QPoint(round(card.left() + 4), round(card.center().y()))
        fill_sample = QPoint(round(card.right() - 12), round(card.bottom() - 8))
        plain = panel.viewport().grab().toImage()
        panel.setCurrentItem(item)
        panel.clearFocus()
        settle(app)
        selected = panel.viewport().grab().toImage()

        canvas = CalendarCanvas()
        widgets.append(canvas)
        canvas.set_preferences(preferences)
        canvas.set_data([START.date()], [entry], [source])
        canvas.selected_id = entry.id
        native = QImage(280, 80, QImage.Format.Format_ARGB32_Premultiplied)
        native.fill('#ffffff')
        painter = QPainter(native)
        canvas.paint_event(painter, QRectF(10, 10, 250, 50), entry, 1)
        painter.end()
        native_bar = native.pixelColor(14, 35)
        assert native_bar.name() == event_colors(source.color, scheme).selected_outline
        assert selected.pixelColor(bar_sample) == native_bar
        assert plain.pixelColor(bar_sample) != native_bar
        assert selected.pixelColor(fill_sample) == plain.pixelColor(fill_sample)
