"""System tray + always-visible floating pill (Wispr Flow-style).

Layout:
  Idle:        [ . . . . . . . . . . ]      <- small pill at bottom-center
  Idle+hover:  Click or hold Ctrl + Win to start dictating   <- tooltip above
               [ . . . . . . . . . . ]
  Recording:   [ (X)  ........ wave .......  (■) ]
  Processing:  [        ◐ scanning dots         ]

Click the idle pill to start recording. Drag any part of it to reposition.
The window never takes focus — Ctrl+V from the paste step lands wherever
you were typing before.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Callable, Optional

APP_DIR = Path(__file__).resolve().parent

from PyQt6.QtCore import (
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QWidget,
)

from config import UiConfig


class State:
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    PROCESSING = "processing"


# ----------------------------- icon helpers ------------------------------


def _make_tray_icon(color: tuple[int, int, int], size: int = 32) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(*color))
    m = 4
    p.drawEllipse(m, m, size - 2 * m, size - 2 * m)
    p.end()
    return QIcon(pix)


def _make_x_icon(px: int = 10) -> QIcon:
    pix = QPixmap(px, px)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(255, 255, 255), 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    m = 2.5
    p.drawLine(int(m), int(m), int(px - m), int(px - m))
    p.drawLine(int(px - m), int(m), int(m), int(px - m))
    p.end()
    return QIcon(pix)


def _make_stop_square_icon(px: int = 8) -> QIcon:
    pix = QPixmap(px, px)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255))
    p.drawRoundedRect(0, 0, px, px, 1.2, 1.2)
    p.end()
    return QIcon(pix)


# ----------------------------- waveform ---------------------------------


class WaveformWidget(QWidget):
    """A row of vertical bars that extend up/down based on mic level.

    Each bar is a rounded vertical capsule centered on the widget midline.
    When silent, every bar collapses to a tiny dot. When voice is detected,
    bars dance in a phase-offset wave whose overall amplitude scales with
    the current audio level — louder voice => taller bars, quieter voice
    => shorter bars. The bars don't scroll horizontally; positions are
    fixed and only their heights change.
    """

    _NOISE_GATE = 0.07
    _BAR_COUNT = 12

    def __init__(self):
        super().__init__()
        self._mode = "static"  # static | reactive | scan
        self._level = 0.0       # smoothed amplitude 0..1
        self._phase = 0.0       # advances when active
        self._scan_pos = -0.2
        self.setFixedHeight(14)
        self.setMinimumWidth(self._BAR_COUNT * 4 + 4)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ---------------- modes ----------------

    def go_static(self) -> None:
        self._mode = "static"
        self._level = 0.0
        self._timer.stop()
        self.update()

    def go_reactive(self) -> None:
        self._mode = "reactive"
        self._level = 0.0
        self._phase = 0.0
        # Animate independently of audio polling so bars always look alive
        # while voice is coming in; level just modulates amplitude.
        self._timer.start(40)

    def go_scan(self) -> None:
        self._mode = "scan"
        self._scan_pos = -0.2
        self._level = 0.0
        self._timer.start(40)

    def set_level(self, level: float) -> None:
        """Push a new audio-level sample (called ~30Hz from the daemon)."""
        if self._mode != "reactive":
            return
        target = 0.0 if level < self._NOISE_GATE else min(1.0, level)
        # Quick attack, gentle release so bars don't twitch on every poll.
        if target > self._level:
            self._level = self._level * 0.3 + target * 0.7
        else:
            self._level = self._level * 0.75 + target * 0.25

    # ---------------- draw ----------------

    def _tick(self) -> None:
        if self._mode == "reactive":
            self._phase += 0.32
        elif self._mode == "scan":
            self._scan_pos += 0.045
            if self._scan_pos > 1.2:
                self._scan_pos = -0.2
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        spacing = 4.0
        bar_width = 2.0
        bar_min_h = 2.0
        bar_max_h = self.height() - 2  # leaves 1px top/bottom
        cy = self.height() / 2
        total = (self._BAR_COUNT - 1) * spacing
        x0 = (self.width() - total) / 2

        for i in range(self._BAR_COUNT):
            x = x0 + i * spacing
            if self._mode == "reactive":
                # Phase-offset wave gives each bar its own up-and-down beat.
                wave = (math.sin(self._phase - i * 0.55) + 1.0) * 0.5  # 0..1
                # Bars stay flat when silent (level≈0). Loud voice raises
                # the wave; the 0.35+0.65*wave term keeps some variation
                # so bars don't all collapse together at low level.
                bar_h = bar_min_h + (bar_max_h - bar_min_h) * self._level * (0.35 + 0.65 * wave)
                alpha = int(150 + 105 * self._level)
            elif self._mode == "scan":
                # A bright peak slides across to indicate "transcribing".
                frac = i / max(1, self._BAR_COUNT - 1)
                d = abs(frac - self._scan_pos)
                bright = max(0.0, 1.0 - d * 4.0)
                bar_h = bar_min_h + (bar_max_h - bar_min_h) * bright
                alpha = int(80 + 160 * bright)
            else:
                bar_h = bar_min_h
                alpha = 90
            y_top = cy - bar_h / 2
            rect = QRectF(x - bar_width / 2, y_top, bar_width, bar_h)
            radius = bar_width / 2
            p.setBrush(QColor(255, 255, 255, alpha))
            p.drawRoundedRect(rect, radius, radius)

        p.end()


# ----------------------------- tooltip ----------------------------------


class HoverTooltip(QWidget):
    """The 'Click or hold Ctrl + Win to start dictating' bubble above the idle pill."""

    def __init__(self, hotkey_text: str):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        formatted_keys = _format_hotkey_html(hotkey_text)
        html = (
            f'<span style="color: #d8d8de;">Click or hold </span>'
            f'{formatted_keys}'
            f'<span style="color: #d8d8de;"> to start dictating</span>'
        )
        self._label = QLabel(html, self)
        self._label.setStyleSheet(
            "QLabel {"
            "  color: white;"
            "  background-color: rgba(20, 20, 24, 240);"
            "  border-radius: 18px;"
            "  padding: 9px 20px;"
            "  font-family: 'Segoe UI', sans-serif;"
            "  font-size: 13px;"
            "}"
        )
        self._label.setTextFormat(Qt.TextFormat.RichText)
        self._label.adjustSize()
        self.resize(self._label.size())


class NotificationBubble(QWidget):
    """Auto-hiding message bubble that appears above a target widget.
    Works regardless of Windows Focus Assist / notification settings,
    since it's just a Qt frameless window we control."""

    def __init__(self):
        super().__init__()
        # WA_TransparentForMouseEvents on the WIDGET (instead of the
        # WindowTransparentForInput window flag) gives us reliable rendering
        # AND lets mouse clicks pass straight through to whatever is below —
        # critical so that a lingering bubble can never block the pill's
        # cancel / stop buttons.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._label = QLabel(self)
        self._label.setTextFormat(Qt.TextFormat.RichText)
        self._label.setWordWrap(False)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def show_message(
        self,
        html: str,
        anchor: QWidget,
        kind: str = "info",
        duration_ms: int = 5000,
    ) -> None:
        accent = "#ff9d3f" if kind == "warning" else "#9be3ff"
        self._label.setStyleSheet(
            "QLabel {"
            "  color: white;"
            f"  background-color: rgba(20, 20, 24, 245);"
            f"  border: 1px solid {accent};"
            "  border-radius: 14px;"
            "  padding: 10px 18px;"
            "  font-family: 'Segoe UI', sans-serif;"
            "  font-size: 13px;"
            "}"
        )
        self._label.setText(html)
        self._label.adjustSize()
        self.resize(self._label.size())
        # Anchor centered above the target widget.
        ax = anchor.x() + anchor.width() // 2 - self.width() // 2
        ay = anchor.y() - self.height() - 10
        # Clamp to primary screen so the bubble can't end up off-screen if
        # the pill was dragged to an edge.
        screen = QApplication.primaryScreen()
        if screen is not None:
            geom = screen.availableGeometry()
            ax = max(geom.left() + 4, min(ax, geom.right() - self.width() - 4))
            ay = max(geom.top() + 4, ay)
        self.move(ax, ay)
        print(
            f"[ui] bubble.show kind={kind} pos=({ax},{ay}) size={self.width()}x{self.height()}",
            file=sys.stderr,
        )
        self.show()
        self.raise_()
        self._hide_timer.start(duration_ms)


def _format_hotkey_html(text: str) -> str:
    """Render 'ctrl+windows' as 'Ctrl + Win' with pink accent on each key."""
    parts = [p.strip() for p in text.split("+") if p.strip()]
    pretty_map = {
        "ctrl": "Ctrl", "control": "Ctrl",
        "windows": "Win", "win": "Win", "cmd": "Cmd",
        "alt": "Alt", "shift": "Shift", "space": "Space",
        "esc": "Esc", "tab": "Tab", "enter": "Enter",
    }
    accent = "#ff6fb0"
    pieces = []
    for i, p in enumerate(parts):
        pretty = pretty_map.get(p.lower(), p.title())
        pieces.append(
            f'<span style="color:{accent}; font-weight:700;">{pretty}</span>'
        )
    sep = '<span style="color:#888;"> + </span>'
    return sep.join(pieces)


# ----------------------------- pill -------------------------------------


class FloatingPill(QWidget):
    sig_start = pyqtSignal()
    sig_stop = pyqtSignal()
    sig_cancel = pyqtSignal()

    _DRAG_THRESHOLD_PX = 5

    def __init__(self, get_audio_level: Callable[[], float], hotkey_label: str):
        super().__init__()
        self._get_audio_level = get_audio_level

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # No intermediate container — we paint the rounded background
        # ourselves in paintEvent so we get pixel-perfect capsule shape
        # regardless of Qt stylesheet quirks.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(5)

        self._cancel_btn = self._make_round_btn(
            QColor(120, 120, 130, 235),
            QColor(150, 150, 160, 245),
            QColor(95, 95, 105, 245),
            _make_x_icon(),
            tooltip="Cancel",
        )
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)

        self._stop_btn = self._make_round_btn(
            QColor(225, 60, 64, 240),
            QColor(245, 78, 82, 250),
            QColor(190, 42, 46, 250),
            _make_stop_square_icon(),
            tooltip="Stop and paste",
        )
        self._stop_btn.clicked.connect(self._on_stop_clicked)

        self._waveform = WaveformWidget()

        layout.addWidget(self._cancel_btn)
        layout.addWidget(self._waveform)
        layout.addWidget(self._stop_btn)

        self._tooltip = HoverTooltip(hotkey_label)
        self._bubble = NotificationBubble()

        self._state = State.IDLE
        self._level_timer = QTimer(self)
        self._level_timer.timeout.connect(self._poll_level)

        self._drag_pos: Optional[QPoint] = None
        self._press_pos: Optional[QPoint] = None
        self._dragging = False

        self._apply_state(State.IDLE)
        self._position_default()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 20, 24, 230))
        r = self.height() / 2
        p.drawRoundedRect(QRectF(self.rect()), r, r)
        p.end()

    def _on_cancel_clicked(self) -> None:
        print(
            f"[ui] cancel button clicked (state={self._state})",
            file=sys.stderr,
        )
        self.sig_cancel.emit()

    def _on_stop_clicked(self) -> None:
        print(
            f"[ui] stop button clicked (state={self._state})",
            file=sys.stderr,
        )
        self.sig_stop.emit()

    # -------------------- styled circular button --------------------

    @staticmethod
    def _make_round_btn(
        bg: QColor, hover: QColor, pressed: QColor, icon: QIcon, tooltip: str
    ) -> QPushButton:
        btn = QPushButton()
        btn.setIcon(icon)
        btn.setIconSize(QSize(9, 9))
        btn.setFixedSize(16, 16)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setStyleSheet(
            "QPushButton {"
            f"  background-color: rgba({bg.red()}, {bg.green()}, {bg.blue()}, {bg.alpha()});"
            "  border: none;"
            "  border-radius: 8px;"
            "}"
            "QPushButton:hover {"
            f"  background-color: rgba({hover.red()}, {hover.green()}, {hover.blue()}, {hover.alpha()});"
            "}"
            "QPushButton:pressed {"
            f"  background-color: rgba({pressed.red()}, {pressed.green()}, {pressed.blue()}, {pressed.alpha()});"
            "}"
        )
        return btn

    # ----------------------- state / layout -------------------------

    def _position_default(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        self.adjustSize()
        x = geom.center().x() - self.width() // 2
        y = geom.bottom() - self.height() - 16
        self.move(x, y)

    def _apply_state(self, state: str) -> None:
        was_visible = self.isVisible()
        prev_bottom_center = (
            self.x() + self.width() // 2,
            self.y() + self.height(),
        ) if was_visible else None

        self._state = state
        if state == State.IDLE:
            self._cancel_btn.setVisible(False)
            self._stop_btn.setVisible(False)
            self._waveform.go_static()
            self._level_timer.stop()
        elif state == State.RECORDING:
            self._tooltip.hide()
            self._cancel_btn.setVisible(True)
            self._stop_btn.setVisible(True)
            self._cancel_btn.setEnabled(True)
            self._stop_btn.setEnabled(True)
            self._cancel_btn.raise_()
            self._stop_btn.raise_()
            self._waveform.go_reactive()
            self._level_timer.start(35)
        elif state == State.PAUSED:
            self._cancel_btn.setVisible(True)
            self._stop_btn.setVisible(True)
            self._cancel_btn.setEnabled(True)
            self._stop_btn.setEnabled(True)
            self._waveform.go_static()
            self._level_timer.stop()
        elif state == State.PROCESSING:
            self._cancel_btn.setVisible(False)
            self._stop_btn.setVisible(False)
            self._waveform.go_scan()
            self._level_timer.stop()

        # Force the layout to recompute so the new button bounds are correct
        # for hit-testing before the user can click again.
        if self.layout() is not None:
            self.layout().activate()
        self.adjustSize()

        # Keep bottom-center anchored so the pill doesn't visually jump
        # when buttons appear/disappear.
        if prev_bottom_center is not None:
            cx, by = prev_bottom_center
            self.move(cx - self.width() // 2, by - self.height())

    def _poll_level(self) -> None:
        try:
            self._waveform.set_level(self._get_audio_level())
        except Exception:
            pass

    # ----------------------- hover tooltip --------------------------

    def enterEvent(self, event) -> None:  # type: ignore[override]
        if self._state == State.IDLE:
            self._show_tooltip()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._tooltip.hide()
        super().leaveEvent(event)

    def _show_tooltip(self) -> None:
        self._tooltip.adjustSize()
        x = self.x() + self.width() // 2 - self._tooltip.width() // 2
        y = self.y() - self._tooltip.height() - 10
        self._tooltip.move(x, y)
        self._tooltip.show()
        self._tooltip.raise_()

    # ---------------------- drag-vs-click ---------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._drag_pos = self._press_pos - self.frameGeometry().topLeft()
            self._dragging = False
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._drag_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            pos = event.globalPosition().toPoint()
            if (
                not self._dragging
                and (pos - (self._press_pos or pos)).manhattanLength()
                > self._DRAG_THRESHOLD_PX
            ):
                self._dragging = True
                self._tooltip.hide()
            if self._dragging:
                self.move(pos - self._drag_pos)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        was_drag = self._dragging
        self._drag_pos = None
        self._press_pos = None
        self._dragging = False
        if not was_drag and self._state == State.IDLE and event.button() == Qt.MouseButton.LeftButton:
            # Click on idle pill body → start recording.
            self.sig_start.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ------------------------- public API ---------------------------

    def show_bubble(self, html: str, kind: str = "info", duration_ms: int = 5000) -> None:
        self._bubble.show_message(html, anchor=self, kind=kind, duration_ms=duration_ms)

    def on_idle(self) -> None:
        # Reset visual state aggressively so a cancel mid-LOCKED leaves no
        # stale colours or bubbles. Defer the layout swap by one event-loop
        # tick to avoid the re-entrance fault where a button hides itself
        # while Qt is still dispatching its own click event.
        self._set_locked_style(False)
        self._tooltip.hide()
        self._bubble.hide()
        QTimer.singleShot(0, lambda: self._apply_state(State.IDLE))

    def on_recording(self, resume: bool = False) -> None:
        # Reset locked style FIRST so the stop button is the right colour
        # before _apply_state makes it visible (avoids a one-frame amber flash).
        self._set_locked_style(False)
        self._bubble.hide()
        self._apply_state(State.RECORDING)

    def on_locked(self) -> None:
        """Visual transition to hands-free mode while still recording."""
        if self._state != State.RECORDING:
            self._apply_state(State.RECORDING)
        self._set_locked_style(True)
        # A brief inline bubble so the user knows the toggle landed.
        self.show_bubble(
            '<span style="font-weight:600;color:#ffc53d;">🔒 Hands-free</span>'
            ' <span style="color:#d8d8de;">— release keys, click ■ to stop.</span>',
            kind="info",
            duration_ms=2500,
        )

    def on_paused(self) -> None:
        self._apply_state(State.PAUSED)
        self._set_locked_style(False)

    def on_processing(self) -> None:
        self._apply_state(State.PROCESSING)
        self._set_locked_style(False)

    def _set_locked_style(self, locked: bool) -> None:
        """Recolour the stop button so the pill visibly indicates hands-free."""
        if locked:
            bg = QColor(255, 170, 50, 240)
            hover = QColor(255, 190, 80, 250)
            pressed = QColor(220, 145, 30, 250)
        else:
            bg = QColor(225, 60, 64, 240)
            hover = QColor(245, 78, 82, 250)
            pressed = QColor(190, 42, 46, 250)
        self._stop_btn.setStyleSheet(
            "QPushButton {"
            f"  background-color: rgba({bg.red()}, {bg.green()}, {bg.blue()}, {bg.alpha()});"
            "  border: none;"
            "  border-radius: 8px;"
            "}"
            "QPushButton:hover {"
            f"  background-color: rgba({hover.red()}, {hover.green()}, {hover.blue()}, {hover.alpha()});"
            "}"
            "QPushButton:pressed {"
            f"  background-color: rgba({pressed.red()}, {pressed.green()}, {pressed.blue()}, {pressed.alpha()});"
            "}"
        )


# ----------------------------- tray app ---------------------------------


class UiBridge(QObject):
    sig_recording = pyqtSignal(bool)
    sig_paused = pyqtSignal()
    sig_processing = pyqtSignal()
    sig_idle = pyqtSignal()
    sig_locked = pyqtSignal()  # hands-free mode activated
    sig_quit = pyqtSignal()
    sig_show_window = pyqtSignal()  # bring main window to front
    # title, message, kind ("info" | "warning")
    sig_notify = pyqtSignal(str, str, str)


class TrayApp:
    def __init__(self, cfg: UiConfig, callbacks: dict, get_audio_level, hotkey_label: str):
        self.cfg = cfg
        self._callbacks = callbacks
        self.window = None  # MainWindow, set later via attach_window()

        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # Give Windows a stable AppUserModelID so the taskbar groups the app
        # under our icon instead of the generic python one.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "flowlite.app"
            )
        except Exception:
            pass

        # App-wide window icon (taskbar + alt-tab).
        app_png = APP_DIR / "icons" / "flowlite.png"
        if app_png.exists():
            self.app.setWindowIcon(QIcon(str(app_png)))

        self._icons = {
            State.IDLE: _make_tray_icon((140, 140, 145)),
            State.RECORDING: _make_tray_icon((255, 77, 79)),
            State.PAUSED: _make_tray_icon((255, 197, 61)),
            State.PROCESSING: _make_tray_icon((255, 197, 61)),
        }

        self._tray = QSystemTrayIcon(self._icons[State.IDLE])
        self._tray.setToolTip("flowlite — idle")
        menu = QMenu()
        open_action = QAction("Open flowlite")
        open_action.triggered.connect(self._open_window)
        menu.addAction(open_action)
        menu.addSeparator()
        self._status_action = QAction("Status: idle")
        self._status_action.setEnabled(False)
        menu.addAction(self._status_action)
        show_pill_action = QAction("Show pill")
        show_pill_action.triggered.connect(self._show_pill)
        menu.addAction(show_pill_action)
        menu.addSeparator()
        quit_action = QAction("Quit flowlite")
        quit_action.triggered.connect(self._handle_quit)
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()
        # Re-assert tray visibility shortly after start — Explorer sometimes
        # drops new tray icons during shell startup.
        QTimer.singleShot(3000, self._tray.show)

        self.pill: Optional[FloatingPill] = None
        if cfg.show_pill:
            self.pill = FloatingPill(get_audio_level=get_audio_level, hotkey_label=hotkey_label)
            self.pill.sig_start.connect(self._safe_call("start"))
            self.pill.sig_stop.connect(self._safe_call("stop"))
            self.pill.sig_cancel.connect(self._safe_call("cancel"))
            self.pill.show()

        self.bridge = UiBridge()
        self.bridge.sig_recording.connect(self._on_recording)
        self.bridge.sig_paused.connect(self._on_paused)
        self.bridge.sig_processing.connect(self._on_processing)
        self.bridge.sig_idle.connect(self._on_idle)
        self.bridge.sig_locked.connect(self._on_locked)
        self.bridge.sig_quit.connect(self._handle_quit)
        self.bridge.sig_notify.connect(self._on_notify)
        self.bridge.sig_show_window.connect(self._open_window)

    def _safe_call(self, key: str):
        cb = self._callbacks.get(key)
        return cb if cb is not None else (lambda: None)

    def _show_pill(self) -> None:
        if self.pill:
            self.pill.show()
            self.pill.raise_()

    def attach_window(self, window) -> None:
        self.window = window

    def _open_window(self) -> None:
        if self.window is not None:
            self.window.show_front()

    def _on_tray_activated(self, reason) -> None:
        # Single-click shows pill; double-click opens the main window.
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._open_window()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_pill()

    def _set_status(self, state: str, tooltip: str) -> None:
        self._tray.setIcon(self._icons[state])
        self._tray.setToolTip(f"flowlite — {tooltip}")
        self._status_action.setText(f"Status: {tooltip}")

    def _on_recording(self, resume: bool) -> None:
        self._set_status(State.RECORDING, "recording")
        if self.pill:
            self.pill.on_recording(resume=resume)

    def _on_locked(self) -> None:
        self._set_status(State.RECORDING, "recording (hands-free)")
        if self.pill:
            self.pill.on_locked()

    def _on_paused(self) -> None:
        self._set_status(State.PAUSED, "paused")
        if self.pill:
            self.pill.on_paused()

    def _on_processing(self) -> None:
        self._set_status(State.PROCESSING, "transcribing")
        if self.pill:
            self.pill.on_processing()

    def _on_idle(self) -> None:
        self._set_status(State.IDLE, "idle")
        if self.pill:
            self.pill.on_idle()

    def _on_notify(self, title: str, message: str, kind: str) -> None:
        print(
            f"[ui] notify kind={kind} title={title!r} msg={message!r}",
            file=sys.stderr,
        )
        # Try the OS toast (may be suppressed by Focus Assist).
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if kind == "warning"
            else QSystemTrayIcon.MessageIcon.Information
        )
        try:
            self._tray.showMessage(title, message, icon, 6000)
        except Exception as e:
            print(f"[ui] tray toast failed: {e}", file=sys.stderr)
        # Always also show the in-pill bubble so the user actually sees it.
        if self.pill:
            html = (
                f'<span style="font-weight:600;">{title}</span><br/>'
                f'<span style="color:#d8d8de;">{message}</span>'
            )
            self.pill.show_bubble(html, kind=kind, duration_ms=6000)

    def _handle_quit(self) -> None:
        try:
            cb = self._callbacks.get("quit")
            if cb:
                cb()
        finally:
            self.app.quit()

    def run(self) -> int:
        return self.app.exec()
