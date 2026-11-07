"""Disposable sidebar alignment studies; never imported by Debacal itself.

Apply to a fresh sample window after its search/header treatment is installed.
The delegates retain real tree rows and native disclosure hit areas. They alter
only presentation; the occurrence model and selection signals remain untouched.
"""
from types import SimpleNamespace

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPen, QPolygonF
from PySide6.QtWidgets import QBoxLayout, QHBoxLayout, QStyle, QStyleOptionViewItem, QWidget

from debacal.agenda_style import AgendaResultDelegate
from debacal.event_style import event_colors
from debacal.search_panel import COLOR_ROLE, EVENT_ROLE, SUBTITLE_ROLE
from debacal.theme import scheme_for


OPTIONS = {
    'a3': {
        'name': 'L1 · Separate time column',
        'thesis': 'Source squares, section titles, search and day headings share the outer edge. '
                  'A3 event boxes start after a real time column.',
        'tradeoff': 'Preserves a clear clock rail and longer one-line titles. Event boxes intentionally '
                    'have a later left edge than source controls and headings.',
    },
    'flush': {
        'name': 'L2 · One outer card edge',
        'thesis': 'Source squares, search, headings and event-box frames share the outer edge. '
                  'The clock and timeline move inside the event frame.',
        'tradeoff': 'Creates an unmistakable common vertical guide. Repeating a frame around the '
                    'clock adds edges and leaves a narrower title region inside each card.',
    },
    'utility': {
        'name': 'L3 · Utility lane and content edge',
        'thesis': 'A dedicated lane holds source squares, read-only locks, times and tree disclosures. '
                  'Calendar names, search, day headings and event frames share a second content edge.',
        'tradeoff': 'The indentation has a concrete job and the content edge is consistent. It spends '
                    'more width on structure, especially at large text sizes. The Calendars heading stays '
                    'on the outer guide; the agenda heading follows its content column.',
    },
}


def _scale(widget):
    return getattr(getattr(widget, 'preferences', None), 'font_scale', 100) / 100


def _small(font, scale):
    result = QFont(font)
    if result.pixelSize() > 0:
        result.setPixelSize(max(9, result.pixelSize() - round(scale)))
    elif result.pointSizeF() > 0:
        result.setPointSizeF(max(7, result.pointSizeF() - scale))
    return result


class AlignmentDelegate(AgendaResultDelegate):
    def __init__(self, parent, direction):
        super().__init__(parent, subtitle_role=SUBTITLE_ROLE, color_role=COLOR_ROLE, event_role=EVENT_ROLE)
        self.direction = direction

    def timeline_offset(self):
        """Use production clock metrics, including All day and 12-hour labels."""
        widget = self.parent()
        option = QStyleOptionViewItem()
        option.initFrom(widget)
        option.font = widget.font()
        option.widget = widget
        option.rect = QRect(0, 0, widget.viewport().width(), 100)
        body, card, _ = super().geometry(option, SimpleNamespace(heading=''))
        return max(59 * _scale(widget), card.left() - body.left())

    def _root_left(self, option, index):
        """Only actual child occurrences indent; top-level objects use the guide."""
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        return depth * self.parent().indentation()

    def row_geometry(self, option, index, row):
        scale = _scale(self.parent())
        body = QRectF(option.rect)
        body.setLeft(self._root_left(option, index))
        if row is not None and row.heading:
            body.setTop(body.top() + 20 * scale)
        card = body.adjusted(.5, 3 * scale, -.5, -3 * scale)
        offset = self.timeline_offset()
        rail = body.left() + offset - 9 * scale if row is not None else None
        content = card.left() + 8 * scale
        if row is not None:
            if self.direction != 'flush':
                card.setLeft(body.left() + offset)
            content = body.left() + offset + 8 * scale
        elif self.direction == 'utility':
            card.setLeft(body.left() + offset)
            content = card.left() + 8 * scale
        elif index.model().hasChildren(index):
            # The native branch target stays in its original gutter, now inside
            # the full-width root frame. Give its chevron real internal space.
            content = max(content, option.rect.left() + 8 * scale)
        return body, card, rail, content

    def paint(self, painter, option, index):
        widget, scale = self.parent(), _scale(self.parent())
        scheme = scheme_for(widget)
        row = self.row_layout(index)
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        focused = bool(styled.state & QStyle.StateFlag.State_HasFocus and widget.hasFocus())
        colors = event_colors(index.data(self.color_role) or scheme.accent, scheme)
        body, card, rail, content = self.row_geometry(option, index, row)
        if card.width() <= 0 or card.height() <= 0:
            return
        small = _small(option.font, scale)
        painter.save()
        # Extend through the top-level branch gutter without changing native
        # tree geometry. Child cards retain their real occurrence indentation.
        painter.setClipRect(QRectF(0, option.rect.top(), widget.viewport().width(), option.rect.height()))
        if row is not None and row.heading:
            heading = QFont(small)
            heading.setBold(True)
            painter.setFont(heading)
            painter.setPen(QColor(scheme.muted))
            left = body.left() + (self.timeline_offset() if self.direction == 'utility' else 0)
            painter.drawText(QPointF(left, option.rect.top() + 14 * scale),
                             painter.fontMetrics().elidedText(row.heading, Qt.TextElideMode.ElideRight,
                                                             max(0, int(option.rect.right() - left))))

        if rail is not None and self.direction != 'flush':
            painter.setPen(QPen(QColor(scheme.control_edge), scale))
            middle = body.center().y()
            painter.drawLine(QPointF(rail, middle if row.first else body.top()),
                             QPointF(rail, middle if row.last else body.bottom()))

        painter.setBrush(QColor(colors.fill))
        painter.setPen(QPen(QColor(colors.selected_outline if selected else colors.outline),
                            (2 if selected else 1) * scale))
        painter.drawRoundedRect(card, 4 * scale, 4 * scale)
        if focused:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(scheme.focus), 2 * scale, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(card.adjusted(3 * scale, 3 * scale, -3 * scale, -3 * scale),
                                    2 * scale, 2 * scale)
        if rail is not None:
            middle = body.center().y()
            if self.direction == 'flush':
                painter.setPen(QPen(QColor(colors.outline), scale))
                painter.drawLine(QPointF(rail, card.top() + 4 * scale),
                                 QPointF(rail, card.bottom() - 4 * scale))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors.selected_outline if selected else colors.outline))
            painter.drawEllipse(QPointF(rail, middle), 2.7 * scale, 2.7 * scale)
            painter.setFont(small)
            painter.setPen(QColor(colors.ink if self.direction == 'flush' else scheme.muted))
            painter.drawText(QRectF(body.left(), body.top(), rail - body.left() - 6 * scale, body.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, row.clock)

        title = QFont(option.font)
        title.setBold(True)
        painter.setFont(title)
        painter.setPen(QColor(colors.ink))
        title_y = card.top() + (17 if row is None or row.body_height == 49 else 21) * scale
        width = max(0, int(card.right() - content - 8 * scale))
        painter.drawText(QPointF(content, title_y),
                         painter.fontMetrics().elidedText(styled.text, Qt.TextElideMode.ElideRight, width))
        if row is None or row.body_height == 49:
            detail = row.detail if row is not None else str(index.data(self.subtitle_role) or '')
            painter.setFont(small)
            painter.setPen(QColor(colors.muted))
            painter.drawText(QPointF(content, card.top() + 34 * scale),
                             painter.fontMetrics().elidedText(detail, Qt.TextElideMode.ElideRight, width))
        painter.restore()

        if index.model().hasChildren(index):
            painter.save()
            x = option.rect.left() - widget.indentation() / 2
            y = option.rect.center().y()
            points = ([(x - 3 * scale, y - 1.5 * scale), (x, y + 1.5 * scale),
                       (x + 3 * scale, y - 1.5 * scale)] if widget.isExpanded(index) else
                      [(x - 1.5 * scale, y - 3 * scale), (x + 1.5 * scale, y),
                       (x - 1.5 * scale, y + 3 * scale)])
            painter.setPen(QPen(QColor(scheme.muted), 1.3 * scale))
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
            painter.restore()


def _containing_layout(layout, target):
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is target:
            return layout
        nested = item.layout()
        if nested is None and item.widget() is not None:
            nested = item.widget().layout()
        if nested is not None and (found := _containing_layout(nested, target)) is not None:
            return found
    return None


def _utility_lane(window, offset):
    scale = _scale(window)
    for row in window.calendar_rows.values():
        layout = row.layout()
        utility = QWidget(row)
        # Round the combined lane and gap once, so the source text and search
        # field keep the same device-pixel edge at 120–140% text sizes too.
        utility.setFixedWidth(round(offset) - round(8 * scale))
        utility.setStyleSheet('background: transparent; border: none;')
        icons = QHBoxLayout(utility)
        icons.setContentsMargins(0, 0, 0, 0)
        icons.setSpacing(round(4 * scale))
        for widget in (row.swatch, getattr(row, 'lock', None)):
            if widget is not None:
                hidden = widget.isHidden()
                layout.removeWidget(widget)
                icons.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
                widget.setVisible(not hidden)
        icons.addStretch()
        layout.insertWidget(0, utility)
        layout.setSpacing(round(8 * scale))
        layout.setContentsMargins(0, 0, 0, 0)
    # Search and its heading share a meaningful content edge after the utility
    # lane. Keep the result viewport full-width so actual times/disclosures can
    # occupy that lane rather than merely adding empty padding everywhere.
    moved = set()
    header = getattr(window, 'search_header', None)
    if header is not None:
        for page in (header.resting, header.active):
            layout = page.layout()
            margins = layout.contentsMargins()
            layout.setContentsMargins(round(offset), margins.top(), margins.right(), margins.bottom())
            moved.add(id(layout))
    for target in (window.search, window.results_heading):
        layout = _containing_layout(window.agenda_section.layout(), target)
        if layout is not None and id(layout) not in moved:
            if isinstance(layout, QBoxLayout) and layout.direction() in (
                    QBoxLayout.Direction.LeftToRight, QBoxLayout.Direction.RightToLeft):
                margins = layout.contentsMargins()
                layout.setContentsMargins(round(offset), margins.top(), margins.right(), margins.bottom())
                moved.add(id(layout))


def apply(window, option):
    """Apply one presentation hypothesis to this disposable sample instance."""
    if option not in OPTIONS:
        raise ValueError('Unknown sidebar alignment option: ' + str(option))
    if getattr(window, '_sidebar_alignment_option', None) is not None:
        raise ValueError('Use a fresh sample window for each alignment option.')
    window._sidebar_alignment_option = option
    for section in (window.calendar_section, window.agenda_section):
        margins = section.layout().contentsMargins()
        section.layout().setContentsMargins(18, margins.top(), 14, margins.bottom())
    window.calendar_box.setContentsMargins(0, 0, 0, 0)
    old = window.results.itemDelegate()
    delegate = AlignmentDelegate(window.results, option)
    if hasattr(old, 'presentation'):
        delegate.presentation = old.presentation
    if option == 'utility':
        _utility_lane(window, delegate.timeline_offset())
    window.results.setItemDelegate(delegate)
    window.results.refresh_agenda_presentation()
    window.results.doItemsLayout()
    window.results.viewport().update()
    window.sidebar.layout().activate()


def alignment_metrics(window):
    """Report measured native edges for the gallery's alignment annotations."""
    root = window.sidebar
    absolute = lambda widget: widget.mapTo(root, QPoint(0, 0)).x()
    data = {
        'source_square': absolute(next(iter(window.calendar_rows.values())).swatch),
        'source_text_control': absolute(next(iter(window.calendar_rows.values())).choice),
        'search_field': absolute(window.search),
        'results_heading': absolute(window.results_heading),
        'result_viewport': absolute(window.results.viewport()),
    }
    item = window.results.topLevelItem(0)
    if item is not None:
        delegate = window.results.itemDelegate()
        option = QStyleOptionViewItem()
        option.initFrom(window.results)
        option.rect = window.results.visualItemRect(item)
        row = delegate.row_layout(window.results.indexFromItem(item))
        body, card, rail, _ = delegate.row_geometry(option, window.results.indexFromItem(item), row)
        data['event_frame'] = round(data['result_viewport'] + card.left(), 1)
        if row is not None:
            data['day_heading'] = round(data['result_viewport'] + body.left()
                                        + (delegate.timeline_offset() if delegate.direction == 'utility' else 0), 1)
    return data
