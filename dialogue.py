from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout


class DialogueBubble(QWidget):
    """Rounded speech bubble that auto-closes after ttl_ms."""

    PADDING = 12
    TAIL_HEIGHT = 10

    def __init__(self, text: str, ttl_ms: int, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._label = QLabel(text, self)
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            "color: #333333; font-size: 14px; font-family: 'PingFang SC', 'Hiragino Sans GB', sans-serif;"
        )
        self._label.setMaximumWidth(220)
        self._label.adjustSize()

        content_w = min(220, self._label.sizeHint().width()) + self.PADDING * 2
        content_h = self._label.sizeHint().height() + self.PADDING * 2
        total_h = content_h + self.TAIL_HEIGHT

        self.setFixedSize(content_w, total_h)
        self._label.setGeometry(
            self.PADDING, self.PADDING,
            content_w - self.PADDING * 2,
            content_h - self.PADDING * 2,
        )

        QTimer.singleShot(ttl_ms, self.close)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        bubble_h = self.height() - self.TAIL_HEIGHT
        w = self.width()
        r = 12  # corner radius

        path = QPainterPath()
        path.addRoundedRect(0, 0, w, bubble_h, r, r)

        # Tail: small triangle pointing downward from center-bottom
        mid = w / 2
        tail_y = bubble_h
        path.moveTo(mid - 8, tail_y)
        path.lineTo(mid, tail_y + self.TAIL_HEIGHT)
        path.lineTo(mid + 8, tail_y)
        path.closeSubpath()

        p.setPen(QPen(QColor("#cccccc"), 1))
        p.setBrush(QColor(255, 255, 255, 230))
        p.drawPath(path)
