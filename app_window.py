"""Main flowlite window — a real app you can open from a shortcut.

Flow-style: left sidebar (Home / Dictionary / Settings), big content area.
Shows live recording status, total words, and lets you edit the dictionary
and hotkeys without touching files.

The window is independent of the floating pill — closing it just hides it
(app keeps running in the tray). Opening it brings it to the front.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
ICON_PNG = APP_DIR / "icons" / "flowlite.png"

_BG = "#f4f3ee"
_CARD = "#ffffff"
_INK = "#1b1b1f"
_MUTED = "#6b6b73"
_ACCENT = "#2dbfa8"  # teal
_SIDEBAR = "#efeee9"


class AppBridge(QObject):
    """Thread-safe status updates from the daemon to the window."""
    sig_status = pyqtSignal(str)        # "idle" | "recording" | "transcribing"
    sig_words = pyqtSignal(int)         # total words dictated
    sig_last = pyqtSignal(str)          # last transcript text


class MainWindow(QMainWindow):
    def __init__(self, dictionary, cfg, on_quit: Callable[[], None]):
        super().__init__()
        self.dictionary = dictionary
        self.cfg = cfg
        self._on_quit = on_quit
        self._total_words = 0

        self.setWindowTitle("flowlite")
        if ICON_PNG.exists():
            self.setWindowIcon(QIcon(str(ICON_PNG)))
        self.resize(900, 600)
        self.setStyleSheet(f"QMainWindow {{ background: {_BG}; }}")

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_home())        # 0
        self._stack.addWidget(self._build_dictionary())   # 1
        self._stack.addWidget(self._build_settings())     # 2
        root.addWidget(self._stack, 1)

        self.bridge = AppBridge()
        self.bridge.sig_status.connect(self._on_status)
        self.bridge.sig_words.connect(self._on_words)
        self.bridge.sig_last.connect(self._on_last)

    # ------------------------------ sidebar ------------------------------

    def _build_sidebar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedWidth(210)
        bar.setStyleSheet(f"background: {_SIDEBAR};")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(16, 20, 16, 16)
        lay.setSpacing(6)

        brand = QLabel("≡≡  flowlite")
        brand.setStyleSheet(f"color: {_INK}; font-size: 18px; font-weight: 800;")
        lay.addWidget(brand)
        lay.addSpacing(18)

        self._nav_buttons = []
        for i, (label, _icon) in enumerate(
            [("Home", ""), ("Dictionary", ""), ("Settings", "")]
        ):
            b = QPushButton(f"   {label}")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(self._nav_style())
            b.clicked.connect(lambda _=False, idx=i: self._navigate(idx))
            lay.addWidget(b)
            self._nav_buttons.append(b)
        self._nav_buttons[0].setChecked(True)

        lay.addStretch(1)

        self._status_chip = QLabel("●  Idle")
        self._status_chip.setStyleSheet(
            f"color: {_MUTED}; font-size: 13px; font-weight: 600; padding: 6px 4px;"
        )
        lay.addWidget(self._status_chip)

        quit_btn = QPushButton("   Quit flowlite")
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.setStyleSheet(
            f"QPushButton {{ color: {_MUTED}; text-align: left; border: none;"
            f" padding: 8px; border-radius: 8px; font-size: 13px; }}"
            f"QPushButton:hover {{ background: #e3e2dc; color: {_INK}; }}"
        )
        quit_btn.clicked.connect(self._on_quit)
        lay.addWidget(quit_btn)
        return bar

    def _nav_style(self) -> str:
        return (
            f"QPushButton {{ color: {_MUTED}; text-align: left; border: none;"
            f" padding: 10px 12px; border-radius: 10px; font-size: 14px;"
            f" font-weight: 600; }}"
            f"QPushButton:hover {{ background: #e3e2dc; }}"
            f"QPushButton:checked {{ background: {_CARD}; color: {_INK}; }}"
        )

    def _navigate(self, idx: int) -> None:
        for i, b in enumerate(self._nav_buttons):
            b.setChecked(i == idx)
        if idx == 1:
            self._refresh_dictionary_view()
        self._stack.setCurrentIndex(idx)

    # ------------------------------- home --------------------------------

    def _build_home(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(36, 32, 36, 32)
        lay.setSpacing(18)

        hello = QLabel("Welcome back")
        hello.setStyleSheet(f"color: {_INK}; font-size: 26px; font-weight: 800;")
        lay.addWidget(hello)

        # Big status card
        self._status_card = QLabel()
        self._status_card.setMinimumHeight(120)
        self._status_card.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_status_card("idle")
        lay.addWidget(self._status_card)

        # Stats row
        row = QHBoxLayout()
        row.setSpacing(14)
        self._words_card = self._stat_card("0", "words dictated")
        self._hotkey_card = self._stat_card(
            _pretty(self.cfg.hotkeys.record), "hold to dictate"
        )
        row.addWidget(self._words_card)
        row.addWidget(self._hotkey_card)
        lay.addLayout(row)

        lay.addWidget(self._section_label("Last transcript"))
        self._last_box = QPlainTextEdit()
        self._last_box.setReadOnly(True)
        self._last_box.setStyleSheet(
            f"QPlainTextEdit {{ background: {_CARD}; border: none; border-radius: 12px;"
            f" padding: 14px; color: {_INK}; font-size: 14px; }}"
        )
        lay.addWidget(self._last_box, 1)
        return page

    def _stat_card(self, big: str, small: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {_CARD}; border-radius: 14px; }}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        big_lbl = QLabel(big)
        big_lbl.setObjectName("big")
        big_lbl.setStyleSheet(f"color: {_INK}; font-size: 24px; font-weight: 800;")
        small_lbl = QLabel(small)
        small_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        v.addWidget(big_lbl)
        v.addWidget(small_lbl)
        card._big = big_lbl  # type: ignore[attr-defined]
        return card

    def _set_status_card(self, status: str) -> None:
        colors = {
            "idle": ("#e9e8e2", _MUTED, "●  Ready — hold your hotkey or click the pill"),
            "recording": ("#fde8e8", "#d63d40", "●  Recording…"),
            "transcribing": ("#fdf4e3", "#c98a1f", "◐  Transcribing…"),
        }
        bg, fg, text = colors.get(status, colors["idle"])
        self._status_card.setText(text)
        self._status_card.setStyleSheet(
            f"QLabel {{ background: {bg}; color: {fg}; border-radius: 16px;"
            f" font-size: 18px; font-weight: 700; }}"
        )

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {_MUTED}; font-size: 12px; font-weight: 700;"
            f" letter-spacing: 1px;"
        )
        return lbl

    # ---------------------------- dictionary -----------------------------

    def _build_dictionary(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(36, 32, 36, 32)
        lay.setSpacing(14)

        title = QLabel("Dictionary")
        title.setStyleSheet(f"color: {_INK}; font-size: 26px; font-weight: 800;")
        lay.addWidget(title)

        sub = QLabel(
            "Add names and words flowlite should spell right, or fix words it "
            "keeps mishearing. Format: a bare word, or  heard => meant"
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {_MUTED}; font-size: 13px;")
        lay.addWidget(sub)

        # add-row
        add_row = QHBoxLayout()
        self._dict_input = QLineEdit()
        self._dict_input.setPlaceholderText("e.g.  Yuvansh   or   fluvlite => flowlite")
        self._dict_input.setStyleSheet(
            f"QLineEdit {{ background: {_CARD}; border: 1px solid #ddd; border-radius: 10px;"
            f" padding: 10px 12px; font-size: 14px; color: {_INK}; }}"
        )
        self._dict_input.returnPressed.connect(self._add_dict_entry)
        add_btn = QPushButton("Add")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(
            f"QPushButton {{ background: {_INK}; color: white; border: none;"
            f" border-radius: 10px; padding: 10px 22px; font-weight: 700; }}"
            f"QPushButton:hover {{ background: #333; }}"
        )
        add_btn.clicked.connect(self._add_dict_entry)
        add_row.addWidget(self._dict_input, 1)
        add_row.addWidget(add_btn)
        lay.addLayout(add_row)

        self._dict_list = QListWidget()
        self._dict_list.setStyleSheet(
            f"QListWidget {{ background: {_CARD}; border: none; border-radius: 12px;"
            f" padding: 6px; font-size: 14px; color: {_INK}; }}"
            f"QListWidget::item {{ padding: 8px 10px; border-bottom: 1px solid #f0f0ec; }}"
        )
        lay.addWidget(self._dict_list, 1)
        return page

    def _refresh_dictionary_view(self) -> None:
        self.dictionary.reload()
        self._dict_list.clear()
        for term in self.dictionary.terms:
            self._dict_list.addItem(QListWidgetItem(term))
        for heard, meant in getattr(self.dictionary, "_corrections", []):
            self._dict_list.addItem(QListWidgetItem(f"{heard}  →  {meant}"))

    def _add_dict_entry(self) -> None:
        text = self._dict_input.text().strip()
        if not text:
            return
        if "=>" in text:
            heard, meant = text.split("=>", 1)
            self.dictionary.add_correction(heard.strip(), meant.strip())
        else:
            self.dictionary.add_term(text)
        self._dict_input.clear()
        self._refresh_dictionary_view()

    # ----------------------------- settings ------------------------------

    def _build_settings(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(36, 32, 36, 32)
        lay.setSpacing(14)

        title = QLabel("Settings")
        title.setStyleSheet(f"color: {_INK}; font-size: 26px; font-weight: 800;")
        lay.addWidget(title)

        info = QLabel(
            "Hotkeys and models live in config.toml (next to the app). "
            "Edit it and restart flowlite to apply.\n\n"
            f"• Hold to dictate:  {_pretty(self.cfg.hotkeys.record)}\n"
            f"• Hands-free toggle:  {_pretty(self.cfg.hotkeys.lock_toggle)}\n"
            f"• Cancel:  {_pretty(self.cfg.hotkeys.cancel)}\n"
            f"• Re-paste last:  {_pretty(self.cfg.hotkeys.retrieve_paste)}\n\n"
            f"• Speech model:  {self.cfg.stt.model}\n"
            f"• Cleanup model:  {self.cfg.formatter.model}"
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            f"QLabel {{ background: {_CARD}; border-radius: 12px; padding: 18px;"
            f" color: {_INK}; font-size: 14px; }}"
        )
        lay.addWidget(info)
        lay.addStretch(1)
        return page

    # ----------------------------- bridge --------------------------------

    def _on_status(self, status: str) -> None:
        labels = {
            "idle": ("●  Idle", _MUTED),
            "recording": ("●  Recording", "#d63d40"),
            "transcribing": ("◐  Transcribing", "#c98a1f"),
        }
        text, color = labels.get(status, labels["idle"])
        self._status_chip.setText(text)
        self._status_chip.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 600; padding: 6px 4px;"
        )
        self._set_status_card(status)

    def _on_words(self, total: int) -> None:
        self._total_words = total
        self._words_card._big.setText(f"{total:,}")  # type: ignore[attr-defined]

    def _on_last(self, text: str) -> None:
        self._last_box.setPlainText(text)

    # close just hides — app stays alive in tray
    def closeEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()
        self.hide()

    def show_front(self) -> None:
        self.show()
        self.setWindowState(
            self.windowState() & ~Qt.WindowState.WindowMinimized
            | Qt.WindowState.WindowActive
        )
        self.raise_()
        self.activateWindow()


def _pretty(combo: str) -> str:
    m = {
        "ctrl": "Ctrl", "control": "Ctrl", "windows": "Win", "win": "Win",
        "cmd": "Cmd", "alt": "Alt", "shift": "Shift", "space": "Space",
        "esc": "Esc", "v": "V",
    }
    return " + ".join(m.get(p.strip().lower(), p.strip().title()) for p in combo.split("+"))
