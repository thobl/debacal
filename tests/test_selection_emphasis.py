"""Direct selections have a bar; series-derived companions retain only their border."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from debacal.appearance import stylesheet
from debacal.calendar_widgets import AllDayCanvas, CalendarCanvas, MonthCanvas
from debacal.event_style import event_colors
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.search_panel import SearchPanel
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)
SOURCE = Calendar('source', 'Appointments', '#587c67')


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets = []
    yield app, widgets
    for widget in reversed(widgets):
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def settle(app):
    for _ in range(5):
        app.processEvents()


def sample_event(identity='event'):
    return Event(identity, SOURCE.id, 'An appointment', START, START + timedelta(hours=1))


@pytest.mark.parametrize('surface', ['week', 'month', 'all_day'])
def test_primary_and_secondary_calendar_cards_share_border_but_only_primary_has_a_bar(desktop, surface):
    _, widgets = desktop
    canvas = {'week': CalendarCanvas, 'month': MonthCanvas, 'all_day': AllDayCanvas}[surface]()
    widgets.append(canvas)
    canvas.calendars = {SOURCE.id: SOURCE}
    canvas.days = [START.date()]
    item = sample_event()
    canvas.selected_id = item.id
    canvas.set_selection({item.id, 'another-explicit-event'})
    rect = QRectF(10, 10, 250, 50)

    def render(primary):
        canvas.set_primary_selection({item.id} if primary else {'another-explicit-event'})
        image = QImage(280, 80, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor('white'))
        painter = QPainter(image)
        if surface == 'week':
            canvas.paint_event(painter, rect, item)
        elif surface == 'month':
            canvas._paint_month_event(painter, rect, item)
        else:
            canvas._paint_bar(painter, rect, item)
        painter.end()
        return image

    primary, secondary = render(True), render(False)
    style = event_colors(SOURCE.color, canvas, alpha=40 if surface == 'month' else 45 if surface == 'all_day' else 52)
    assert primary.pixelColor(14, 45).name() == style.selected_outline
    assert secondary.pixelColor(14, 45).name() == style.fill
    assert primary.pixelColor(120, 10).name() == secondary.pixelColor(120, 10).name() == style.selected_outline
    assert primary.pixelColor(240, 45) == secondary.pixelColor(240, 45)


def test_month_selection_does_not_change_label_font_weight(desktop):
    _, widgets = desktop
    canvas = MonthCanvas()
    widgets.append(canvas)
    item = sample_event()
    canvas.calendars = {SOURCE.id: SOURCE}
    canvas.selected_id = item.id
    canvas.set_selection({item.id})
    image = QImage(280, 80, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    with patch('debacal.calendar_widgets.text') as paint_text:
        canvas._paint_month_event(painter, QRectF(10, 10, 250, 50), item)
    painter.end()
    label = next(call for call in paint_text.call_args_list if item.title in str(call.args[2]))
    assert not (label.args[5] if len(label.args) > 5 else label.kwargs.get('bold', False))


@pytest.mark.parametrize('presentation', ['search', 'cards', 'grouped', 'timeline'])
def test_sidebar_primary_and_scope_companion_use_the_same_selection_language(desktop, presentation):
    app, widgets = desktop
    with Store(':memory:') as store:
        store.save_calendar(SOURCE)
        first = sample_event('first')
        second = Event('second', SOURCE.id, 'Another appointment', START + timedelta(hours=3), START + timedelta(hours=4))
        store.apply('Create', [first, second])
        preferences = Preferences(agenda_style='cards' if presentation == 'search' else presentation)
        panel = SearchPanel(store, preferences)
        widgets.append(panel)
        panel.setStyleSheet(stylesheet(preferences.font_scale, preferences))
        panel.set_range(START, START + timedelta(days=7))
        panel.resize(340, 300)
        panel.show()
        if presentation == 'search':
            panel.populate('appointment')
        else:
            panel.populate_upcoming(START)
        panel.set_row_selection({first.id, second.id})
        panel.set_primary_selection({first.id})
        panel.clearFocus()
        settle(app)
        rendered = panel.viewport().grab().toImage()
        style = event_colors(SOURCE.color, preferences)

        for item in panel._all_rows():
            identity = panel.event_id(item)
            if identity not in (first.id, second.id):
                continue
            row = panel.visualItemRect(item)
            delegate = panel.itemDelegate()
            if presentation in ('grouped', 'timeline'):
                option = QStyleOptionViewItem()
                option.initFrom(panel)
                option.widget, option.font, option.rect = panel, panel.font(), row
                _, card, _ = delegate.geometry(option, delegate.row_layout(panel.indexFromItem(item)))
            else:
                card = QRectF(row).adjusted(1, 3, -1, -3)
            bar = QPoint(round(card.left() + 4), round(card.center().y()))
            assert rendered.pixelColor(bar).name() == (style.selected_outline if identity == first.id else style.fill)
            edge = QPoint(round(card.center().x()), round(card.top()))
            assert rendered.pixelColor(edge).name() == style.selected_outline
