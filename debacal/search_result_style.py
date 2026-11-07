"""Source-colored event cards for agenda and search, retaining native tree input."""
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPen, QPolygonF
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QStyledItemDelegate

from .event_style import event_colors
from .theme import scheme_for


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
        focused = bool(styled.state & QStyle.StateFlag.State_HasFocus
                       and widget and widget.hasFocus())
        card = QRectF(option.rect).adjusted(scale, 3 * scale, -scale, -3 * scale)
        if card.width() <= 0 or card.height() <= 0:
            return

        painter.save()
        painter.setClipRect(option.rect)
        painter.setBrush(QColor(colors.fill))
        painter.setPen(QPen(QColor(colors.selected_outline if selected else colors.outline),
                            (2 if selected else 1) * scale))
        painter.drawRoundedRect(card, 4 * scale, 4 * scale)
        if focused:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(scheme.focus), 2 * scale, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(card.adjusted(3 * scale, 3 * scale, -3 * scale, -3 * scale),
                                    2 * scale, 2 * scale)

        left, width = card.left() + 8 * scale, max(0, card.width() - 16 * scale)
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

        # The branch stays in the tree's original gutter, with its native click
        # target. Its color follows this widget's scheme rather than OS highlight.
        if index.model().hasChildren(index):
            painter.save()
            indent = widget.indentation() if widget and hasattr(widget, 'indentation') else 15 * scale
            x, y = option.rect.left() - indent / 2, option.rect.center().y()
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
