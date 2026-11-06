"""Text controls that preserve the layout when their contents get long."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QPushButton, QStyle, QStyleOptionButton, QStylePainter


class ElidedLabel(QLabel):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.contentsRect(), self.alignment(), self.fontMetrics().elidedText(
            self.text().replace('\n', ' '), Qt.TextElideMode.ElideRight, self.contentsRect().width()))


class ElidedButton(QPushButton):
    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.fontMetrics().elidedText(option.text, Qt.TextElideMode.ElideRight,
                                                    max(0, self.width() - 8))
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
