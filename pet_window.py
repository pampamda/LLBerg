import json
import math
import os
import random
import sys
from enum import Enum

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QCursor, QPainter, QPalette, QPixmap, QTransform
from PyQt6.QtWidgets import QApplication, QWidget

from dialogue import DialogueBubble


def resource_path(relative: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


class State(Enum):
    LIE  = "lie"
    LICK = "lick"
    WALK = "walk"
    PET  = "pet"
    TOY  = "toy"


_STATE_CFG: dict[State, dict] = {
    State.LIE:  dict(min=100, max=400, moves=False,
                     next=[(State.LICK, 3), (State.WALK, 5), (State.LIE, 2)]),
    State.LICK: dict(min=40,  max=120, moves=False,
                     next=[(State.LIE, 4), (State.WALK, 6)]),
    State.WALK: dict(min=20,  max=80,  moves=True,
                     next=[(State.LIE, 3), (State.LICK, 3), (State.WALK, 3), (State.TOY, 1)]),
    State.TOY:  dict(min=30,  max=60,  moves=True,
                     next=[(State.WALK, 1)]),
    State.PET:  dict(min=40,  max=40,  moves=False,
                     next=[]),
}

_TRANSPARENT = QColor(0, 0, 0, 0)


class PetWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._current_pixmap: QPixmap | None = None
        self._load_config()
        self._load_sprites()
        self._setup_window()
        self._setup_state()
        self._start_timers()

    # ── loading ────────────────────────────────────────────────────────────

    def _load_config(self):
        with open(resource_path("config.json"), encoding="utf-8") as f:
            self._cfg = json.load(f)

    def _load_sprites(self):
        base = resource_path("assets/sprites")
        size = self._cfg.get("sprite_size", 150)
        self._sprites: dict[State, list[QPixmap]] = {}

        for state in State:
            folder = os.path.join(base, state.value)
            frames: list[QPixmap] = []
            i = 1
            while True:
                path = os.path.join(folder, f"{state.value}_{i:02d}.png")
                if not os.path.exists(path):
                    break
                frames.append(QPixmap(path).scaled(
                    size, size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
                i += 1
            self._sprites[state] = frames

        fallback: list[QPixmap] = []
        for s in (State.LIE, State.LICK, State.WALK):
            if self._sprites.get(s):
                fallback = self._sprites[s]
                break
        if not fallback:
            px = QPixmap(size, size)
            px.fill(Qt.GlobalColor.transparent)
            fallback = [px]
        for state in State:
            if not self._sprites[state]:
                self._sprites[state] = fallback

        self._breath_phase = 0.0
        self._bob_phase = 0.0

    # ── window ─────────────────────────────────────────────────────────────

    def _setup_window(self):
        size = self._cfg.get("sprite_size", 150)
        self.setFixedSize(size, size)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )

        # --- Transparency: three layers must ALL be set ---
        # 1. Tell Qt to use an ARGB backing store with DWM compositing
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 2. Tell Qt NOT to paint a system background before our paintEvent
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # 3. Tell Qt NOT to auto-fill background from palette
        self.setAutoFillBackground(False)
        # 4. Override the QPalette Window role — Qt's style engine reads this
        #    even when autoFillBackground is False, in certain paint paths
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window, _TRANSPARENT)
        pal.setColor(QPalette.ColorRole.Base,   _TRANSPARENT)
        self.setPalette(pal)

        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        screen = QApplication.primaryScreen().availableGeometry()
        margin = self._cfg.get("screen_margin", 80)
        self.move(screen.width() - size - margin, screen.height() - size - margin)

    def showEvent(self, event):
        super().showEvent(event)
        # DWM calls must happen AFTER the window is shown and the HWND is live
        QTimer.singleShot(0, self._apply_windows_dwm_fixes)

    def _apply_windows_dwm_fixes(self):
        """Remove all Windows visual decorations that bleed through transparency."""
        try:
            import ctypes
            hwnd = int(self.winId())

            # Remove Windows 11 rounded corners
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33,                           # DWMWA_WINDOW_CORNER_PREFERENCE
                ctypes.byref(ctypes.c_int(1)),      # DWMWCP_DONOTROUND
                ctypes.sizeof(ctypes.c_int),
            )

            # Remove Windows 11 system backdrop (Mica / Acrylic)
            # This is the most common cause of an unexpected dark background
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 38,                       # DWMWA_SYSTEMBACKDROP_TYPE
                    ctypes.byref(ctypes.c_int(1)),  # DWMSBT_NONE
                    ctypes.sizeof(ctypes.c_int),
                )
            except Exception:
                pass  # attribute 38 only exists on Windows 11 22H2+

            # Remove DWM window shadow by zeroing the non-client frame margins
            class MARGINS(ctypes.Structure):
                _fields_ = [("cxLeftWidth",  ctypes.c_int),
                             ("cxRightWidth", ctypes.c_int),
                             ("cyTopHeight",  ctypes.c_int),
                             ("cyBottomHeight", ctypes.c_int)]
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                hwnd, ctypes.byref(MARGINS(0, 0, 0, 0))
            )

        except Exception:
            pass

    # ── paint ──────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        # CompositionMode_Source copies source RGBA directly to destination.
        # Filling with (0,0,0,0) guarantees every pixel is alpha=0 before we
        # draw the sprite. (Clear mode has subtle driver-level differences on
        # some Windows systems and is less reliable than Source here.)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), _TRANSPARENT)

        if self._current_pixmap and not self._current_pixmap.isNull():
            # SourceOver blends the sprite's own alpha onto the now-transparent bg
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            x = (self.width()  - self._current_pixmap.width())  // 2
            y = (self.height() - self._current_pixmap.height()) // 2
            painter.drawPixmap(x, y, self._current_pixmap)

        painter.end()

    # ── state machine ──────────────────────────────────────────────────────

    def _setup_state(self):
        pos = self.pos()
        self._pos_x  = float(pos.x())
        self._pos_y  = float(pos.y())
        self._home_x = self._pos_x
        self._home_y = self._pos_y

        self._state      = State.LIE
        self._prev_state = State.LIE
        self._frame_idx  = 0
        self._facing_right    = True
        self._ticks_left      = 0
        self._walk_ticks_left = 0
        self._dialogue   = None
        self._is_dragging = False

        self._enter_state(State.LIE)

    def _enter_state(self, state: State, quick: bool = False):
        cfg = _STATE_CFG[state]
        self._state     = state
        self._frame_idx = 0
        self._breath_phase = 0.0

        if cfg["moves"]:
            self._start_walk_segment()
        else:
            self.move(int(self._pos_x), int(self._home_y))
            self._pos_y = self._home_y
            lo, hi = (10, 40) if quick else (cfg["min"], cfg["max"])
            self._ticks_left = random.randint(lo, hi)

    def _transition_next(self):
        cfg = _STATE_CFG[self._state]
        if not cfg["next"]:
            self._enter_state(State.PET if self.underMouse() else self._prev_state)
            return
        states, weights = zip(*cfg["next"])
        self._enter_state(random.choices(states, weights=weights, k=1)[0])

    # ── timers & animation ─────────────────────────────────────────────────

    def _start_timers(self):
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_animation)
        self._anim_timer.start(self._cfg.get("animation_ms", 180))

        self._move_timer = QTimer(self)
        self._move_timer.timeout.connect(self._tick_move)
        self._move_timer.start(self._cfg.get("move_ms", 50))

    def _tick_animation(self):
        if self._is_dragging:
            return
        frames = self._sprites[self._state]
        self._frame_idx = (self._frame_idx + 1) % len(frames)
        px = frames[self._frame_idx]

        if not _STATE_CFG[self._state]["moves"]:
            self._breath_phase += 0.08
            scale = 1.0 + 0.02 * math.sin(self._breath_phase)
        else:
            self._breath_phase = 0.0
            scale = 1.0

        self._current_pixmap = self._transform(px, scale)
        self.update()

    def _transform(self, px: QPixmap, scale: float = 1.0) -> QPixmap:
        cx, cy = px.width() / 2, px.height() / 2
        t = QTransform()
        t.translate(cx, cy)
        t.scale(-scale if not self._facing_right else scale, scale)
        t.translate(-cx, -cy)
        return px.transformed(t, Qt.TransformationMode.SmoothTransformation)

    # ── movement ───────────────────────────────────────────────────────────

    def _tick_move(self):
        if self._is_dragging:
            return
        if _STATE_CFG[self._state]["moves"]:
            self._step_walk()
        else:
            if self._ticks_left > 0:
                self._ticks_left -= 1
            else:
                self._transition_next()

    def _start_walk_segment(self):
        walk_range = self._cfg.get("walk_range", 200)
        offset = self._pos_x - self._home_x
        if abs(offset) > walk_range * 0.7:
            self._walk_dir = -1 if offset > 0 else 1
        else:
            self._walk_dir = random.choice([-1, 1])
        self._facing_right    = self._walk_dir > 0
        self._frame_idx       = 0
        self._bob_phase       = 0.0
        speed = self._cfg.get("walk_speed", 1.5)
        dist  = random.uniform(40, walk_range * 0.8)
        self._walk_ticks_left = max(1, int(dist / speed))

    def _step_walk(self):
        if self._walk_ticks_left <= 0:
            self._transition_next()
            return
        speed      = self._cfg.get("walk_speed", 1.5)
        walk_range = self._cfg.get("walk_range", 200)
        new_x = self._pos_x + self._walk_dir * speed
        if new_x < self._home_x - walk_range or new_x > self._home_x + walk_range:
            self._enter_state(State.LIE, quick=True)
            return
        self._pos_x = new_x
        self._walk_ticks_left -= 1
        self._bob_phase += 0.28
        self.move(int(self._pos_x), int(self._home_y + math.sin(self._bob_phase) * 1.5))

    # ── interaction ────────────────────────────────────────────────────────

    def enterEvent(self, _):
        if self._state != State.PET and not self._is_dragging:
            self._prev_state = self._state
            self._enter_state(State.PET)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_global = event.globalPosition().toPoint()
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._is_dragging = False

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if not hasattr(self, "_drag_offset"):
            return
        delta = event.globalPosition().toPoint() - self._drag_start_global
        if not self._is_dragging and (abs(delta.x()) + abs(delta.y())) > 6:
            self._is_dragging = True
        if self._is_dragging:
            new_pos = event.globalPosition().toPoint() - self._drag_offset
            self.move(new_pos)
            self._pos_x = float(new_pos.x())
            self._pos_y = float(new_pos.y())

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not self._is_dragging:
            self._show_dialogue()
        else:
            self._home_x = self._pos_x
            self._home_y = self._pos_y
            self._enter_state(State.LIE, quick=True)
        self._is_dragging = False
        if hasattr(self, "_drag_offset"):
            del self._drag_offset

    def _show_dialogue(self):
        if self._dialogue and self._dialogue.isVisible():
            self._dialogue.close()
        text = random.choice(self._cfg.get("phrases", ["Meow"]))
        ttl  = self._cfg.get("dialogue_ttl_ms", 3000)
        self._dialogue = DialogueBubble(text, ttl, parent=None)
        bx = self.x() + self.width()  // 2 - self._dialogue.width()  // 2
        by = self.y() - self._dialogue.height() - 8
        self._dialogue.move(bx, max(0, by))
        self._dialogue.show()
