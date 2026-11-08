"""Source-colored event cards for agenda and search, retaining native tree input."""
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPen, QPolygonF
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QStyledItemDelegate

from .event_style import event_colors, paint_event_card
from .theme import scheme_for


def disclosure_rect(rect, scale=1):
    """Use the card's trailing lane for both disclosure painting and clicks."""
    card = QRectF(rect).adjusted(scale, 3 * scale, -scale, -3 * scale)
    width = min(24 * scale, max(0, card.width()))
    return QRectF(card.right() - width, card.top(), width, max(0, card.height()))


class SearchResultDelegate(QStyledItemDelegate):
    """Paint event objects without altering row identity, indentation or hit areas."""

    def __init__(self, parent=None, *, subtitle_role=int(Qt.ItemDataRole.UserRole) + 1,
                 color_role=int(Qt.ItemDataRole.UserRole) + 2):
        super().__init__(parent)
        self.subtitle_role, self.color_role = subtitle_role, color_role

    @staticmethod
    def _scale(widget):
        return getattr(getattr(widget, 'preferences', None), 'font_scale', 100) / 100

    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(),
                     round(49 * self._scale(option.widget or self.parent())))

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        widget = option.widget or self.parent()
        scheme = scheme_for(widget)
        scale = self._scale(widget)
        colors = event_colors(index.data(self.color_role) or scheme.accent, scheme)
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        identity = index.data(Qt.ItemDataRole.UserRole)
        primary = selected and (not hasattr(widget, 'is_primary_selected') or widget.is_primary_selected(identity))
        focused = bool(styled.state & QStyle.StateFlag.State_HasFocus
                       and widget and widget.hasFocus())
        fold = (disclosure_rect(option.rect, scale)
                if not getattr(widget, 'agenda_mode', False) and index.model().hasChildren(index) else None)
        card = QRectF(option.rect).adjusted(scale, 3 * scale, -scale, -3 * scale)
        if card.width() <= 0 or card.height() <= 0:
            return

        painter.save()
        painter.setClipRect(option.rect)
        paint_event_card(painter, card, colors, selected=selected, primary=primary, focused=focused,
                         focus_color=scheme.focus, scale=scale, preferences=widget)

        left, width = card.left() + 8 * scale, max(0, card.width() - 16 * scale)
        if fold is not None:
            width = max(0, width - fold.width())
        normal = QFont(option.font)
        bold = QFont(normal)
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(QColor(colors.ink))
        painter.drawText(QPointF(left, card.top() + 17 * scale),
                         painter.fontMetrics().elidedText(styled.text, Qt.TextElideMode.ElideRight, int(width)))
        if normal.pixelSize() > 0:
            normal.setPixelSize(max(9, normal.pixelSize() - round(scale)))
        elif normal.pointSizeF() > 0:
            normal.setPointSizeF(max(7, normal.pointSizeF() - scale))
        painter.setFont(normal)
        painter.setPen(QColor(colors.muted))
        painter.drawText(QPointF(left, card.top() + 34 * scale),
                         painter.fontMetrics().elidedText(str(index.data(self.subtitle_role) or ''),
                                                         Qt.TextElideMode.ElideRight, int(width)))
        painter.restore()

        # Top-level rows use the same outer edge as the search input. Series
        # keep an explicit disclosure inside the card; dated children retain
        # the tree's native indentation and keyboard behavior.
        if fold is not None:
            painter.save()
            x, y = fold.center().x(), fold.center().y()
            expanded = bool(widget and hasattr(widget, 'isExpanded') and widget.isExpanded(index))
            points = ([(x - 3 * scale, y - 1.5 * scale), (x, y + 1.5 * scale),
                       (x + 3 * scale, y - 1.5 * scale)] if expanded else
                      [(x - 1.5 * scale, y - 3 * scale), (x + 1.5 * scale, y),
                       (x - 1.5 * scale, y + 3 * scale)])
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(scheme.muted), 1.3 * scale, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
            painter.restore()
