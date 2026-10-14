"""Small native controls with consistent, visible hit targets on all platforms."""
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy, QProxyStyle, QStyle, QStyleOptionSpinBox, QSpinBox


class ElidingLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumHeight(20)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.WindowText))
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), Qt.AlignLeft | Qt.AlignVCenter, text)


class AccessibleStyle(QProxyStyle):
    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PE_IndicatorCheckBox:
            return super().drawPrimitive(element, option, painter, widget)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(option.rect).adjusted(1.5, 1.5, -1.5, -1.5)
        checked = bool(option.state & (QStyle.State_On | QStyle.State_NoChange))
        enabled = bool(option.state & QStyle.State_Enabled)
        color = option.palette.color(QPalette.Link if checked else QPalette.PlaceholderText)
        if not enabled:
            color.setAlpha(140)
        painter.setPen(QPen(color, 1.8))
        painter.setBrush(color if checked else option.palette.color(QPalette.Base))
        painter.drawRoundedRect(rect, 3, 3)
        if checked:
            painter.setPen(QPen(option.palette.color(QPalette.Window), 2.2))
            path = QPainterPath()
            path.moveTo(rect.left() + rect.width() * .20, rect.top() + rect.height() * .51)
            path.lineTo(rect.left() + rect.width() * .43, rect.top() + rect.height() * .74)
            path.lineTo(rect.left() + rect.width() * .82, rect.top() + rect.height() * .25)
            painter.drawPath(path)
        painter.restore()

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in (QStyle.PM_IndicatorWidth, QStyle.PM_IndicatorHeight):
            return 20
        return super().pixelMetric(metric, option, widget)


class ReadableSpinBox(QSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(40)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        for subcontrol, direction in ((QStyle.SC_SpinBoxUp, -1), (QStyle.SC_SpinBoxDown, 1)):
            rect = self.style().subControlRect(QStyle.CC_SpinBox, option, subcontrol, self)
            if rect.isEmpty():
                continue
            center = rect.center()
            painter.setPen(QPen(self.palette().color(QPalette.Text if self.isEnabled() else QPalette.PlaceholderText), 1.8))
            path = QPainterPath()
            path.moveTo(center.x() - 4, center.y() - direction * 2)
            path.lineTo(center.x(), center.y() + direction * 2)
            path.lineTo(center.x() + 4, center.y() - direction * 2)
            painter.drawPath(path)
