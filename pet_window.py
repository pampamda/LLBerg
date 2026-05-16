import json
import os
import random
import sys
from enum import Enum

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QCursor, QMovie, QPainter, QPalette, QPixmap, QTransform
from PyQt6.QtWidgets import QApplication, QWidget

from dialogue import DialogueBubble


def resource_path(relative: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


class State(Enum):
    LIE          = "lie"
    LIE_TO_STAND = "lie_to_stand"
    WALK         = "walk"
    STAND_TO_LIE = "stand_to_lie"
    LIE_TO_SIT   = "lie_to_sit"
    SIT_TO_LIE   = "sit_to_lie"   # lie_to_sit frames in reverse; no folder needed
    LICK         = "lick"
    CHILL        = "chill"
    RUN_AWAY     = "run_away"
    EASTER       = "easter"
    CAT_RETURN   = "cat_return"


# State flow:
#
#   Autonomous random loop (weights read from config "idle_next_weights"):
#     lie → stay LIE | walk → LIE_TO_STAND→WALK→STAND_TO_LIE→LIE
#     lick → LIE_TO_SIT→LICK→SIT_TO_LIE→LIE | chill → CHILL→LIE
#
#   User click loop:
#     LIE  + 3 clicks → LIE_TO_SIT → LICK
#     LICK + 3 clicks → RUN_AWAY → EASTER → CAT_RETURN → LIE
#
#   anim_ms  — per-state animation interval (overrides animation_ms from config)
#   moves    — position updated by move tick
#   play_once — animation plays once, then _transition_next() is called
_STATE_CFG: dict[State, dict] = {
    State.LIE:          dict(anim_ms=250, min=600, max=1800, moves=False, play_once=False,
                             next=[(State.LIE, 35), (State.LIE_TO_STAND, 35),
                                   (State.LIE_TO_SIT, 15), (State.CHILL, 15)]),
    State.LIE_TO_STAND: dict(moves=False, play_once=True,  next=[(State.WALK, 1)]),
    State.WALK:         dict(min=20,  max=80,   moves=True,  play_once=False,
                             next=[(State.STAND_TO_LIE, 1)]),
    State.STAND_TO_LIE: dict(moves=False, play_once=True,  next=[(State.LIE, 1)]),
    State.LIE_TO_SIT:   dict(moves=False, play_once=True,  next=[(State.LICK, 1)]),
    State.SIT_TO_LIE:   dict(anim_ms=250, moves=False, play_once=True,  next=[(State.LIE, 1)]),
    State.LICK:         dict(anim_ms=250, min=80,  max=200,  moves=False, play_once=False,
                             next=[(State.SIT_TO_LIE, 1)]),
    State.CHILL:        dict(anim_ms=250, min=80,  max=240,  moves=False, play_once=False,
                             next=[(State.LIE, 1)]),
    State.RUN_AWAY:     dict(moves=True,  play_once=False,  next=[(State.EASTER, 1)]),
    State.EASTER:       dict(min=100, max=200,  moves=False, play_once=False,
                             next=[(State.CAT_RETURN, 1)]),
    State.CAT_RETURN:   dict(moves=True,  play_once=False,  next=[(State.LIE, 1)]),
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

        # States that share another state's sprite folder (or have no folder at all)
        _folder_override = {
            State.CAT_RETURN: "walk",   # walk back reuses walk sprites (mirrored)
            State.SIT_TO_LIE: None,     # no folder; frames built from lie_to_sit in reverse
        }

        for state in State:
            folder_name = _folder_override.get(state, state.value)
            frames: list[QPixmap] = []
            if folder_name is not None:
                folder = os.path.join(base, folder_name)
                if os.path.isdir(folder):
                    for fname in sorted(os.listdir(folder)):
                        if fname.lower().endswith(".png"):
                            frames.append(QPixmap(os.path.join(folder, fname)).scaled(
                                size, size,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            ))
            self._sprites[state] = frames

        # Per-state fallbacks when sprites are missing
        lie  = self._sprites.get(State.LIE,  [])
        walk = self._sprites.get(State.WALK, [])
        lick = self._sprites.get(State.LICK, [])
        _fallbacks = {
            State.LIE_TO_STAND: lie  or walk,
            State.STAND_TO_LIE: walk or lie,
            State.LIE_TO_SIT:   lie,
            State.CHILL:        lie,
            State.RUN_AWAY:     lick or walk or lie,
            State.EASTER:       lick or lie,
            State.CAT_RETURN:   walk or lie,
        }
        for state, fallback in _fallbacks.items():
            if not self._sprites[state]:
                self._sprites[state] = fallback

        # SIT_TO_LIE = lie_to_sit played in reverse (sit → lie transition)
        self._sprites[State.SIT_TO_LIE] = (
            list(reversed(self._sprites[State.LIE_TO_SIT]))
            or lie
        )

        # Last-resort: any non-empty list, or a blank frame
        final: list[QPixmap] = lie or lick or walk or []
        if not final:
            px = QPixmap(size, size)
            px.fill(Qt.GlobalColor.transparent)
            final = [px]
        for state in State:
            if not self._sprites[state]:
                self._sprites[state] = final


    # ── window ─────────────────────────────────────────────────────────────

    def _setup_window(self):
        size = self._cfg.get("sprite_size", 150)
        self.setFixedSize(size, size)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
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
        QTimer.singleShot(0, self._apply_windows_dwm_fixes)

    def _apply_windows_dwm_fixes(self):
        """Remove Windows visual decorations that bleed through transparency."""
        try:
            import ctypes
            hwnd = int(self.winId())

            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33,
                ctypes.byref(ctypes.c_int(1)),
                ctypes.sizeof(ctypes.c_int),
            )
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 38,
                    ctypes.byref(ctypes.c_int(1)),
                    ctypes.sizeof(ctypes.c_int),
                )
            except Exception:
                pass

            class MARGINS(ctypes.Structure):
                _fields_ = [("cxLeftWidth",    ctypes.c_int),
                             ("cxRightWidth",   ctypes.c_int),
                             ("cyTopHeight",    ctypes.c_int),
                             ("cyBottomHeight", ctypes.c_int)]
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                hwnd, ctypes.byref(MARGINS(0, 0, 0, 0))
            )
        except Exception:
            pass

    # ── paint ──────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), _TRANSPARENT)

        if self._current_pixmap and not self._current_pixmap.isNull():
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

        self._state        = State.LIE
        self._frame_idx    = 0
        self._facing_right = True
        self._ticks_left      = 0
        self._walk_ticks_left = 0
        self._walk_dir        = 1
        self._run_speed       = 0.0
        self._dialogue        = None
        self._is_dragging     = False

        self._walk_primary_facing = True

        self._click_count = 0
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(10_000)
        self._click_timer.timeout.connect(self._reset_click_count)

        self._easter_movie:      QMovie | None = None
        self._easter_loops:      int           = 0
        self._easter_frame_prev: int           = -1
        self._last_easter_gif:   str | None    = None

        self._enter_state(State.LIE)

    def _reset_click_count(self):
        self._click_count = 0

    def _enter_state(self, state: State, quick: bool = False):
        print(f"[state] {getattr(self, '_state', '?').name if hasattr(self, '_state') else '?'} → {state.name}")

        # Stop any running Easter GIF before switching state
        if self._easter_movie is not None:
            self._easter_movie.stop()
            self._easter_movie.frameChanged.disconnect(self._on_easter_frame)
            self._easter_movie = None

        self._state        = state
        self._frame_idx    = 0
        cfg = _STATE_CFG[state]

        if hasattr(self, "_anim_timer"):
            interval = cfg.get("anim_ms", self._cfg.get("animation_ms", 180))
            self._anim_timer.setInterval(interval)

        if state == State.RUN_AWAY:
            self._run_speed    = self._cfg.get("walk_speed", 1.5)
            self._facing_right = True

        elif state == State.CAT_RETURN:
            # Spawn just off the right edge (where the cat ran to) and walk left home
            screen = QApplication.primaryScreen().availableGeometry()
            self._pos_x = float(screen.width())
            self._pos_y = self._home_y
            self.move(int(self._pos_x), int(self._pos_y))
            self._facing_right    = False
            speed = self._cfg.get("return_speed", self._cfg.get("walk_speed", 1.5) * 2)
            self._walk_ticks_left = max(1, int((self._pos_x - self._home_x) / speed))

        elif state == State.EASTER:
            # Always return to home — cat is off-screen after RUN_AWAY
            self._pos_x = self._home_x
            self._pos_y = self._home_y
            self.move(int(self._home_x), int(self._home_y))
            self._start_easter_gif()

        elif cfg["moves"]:  # WALK
            self._start_walk_segment()

        else:
            self.move(int(self._pos_x), int(self._home_y))
            self._pos_y = self._home_y
            if not cfg["play_once"]:
                lo = cfg.get("min", 600)
                hi = cfg.get("max", 1800)
                if state == State.LIE:
                    lo = self._cfg.get("idle_min_ticks", lo)
                    hi = self._cfg.get("idle_max_ticks", hi)
                if quick:
                    lo, hi = 10, 40
                scale = self._cfg.get("tick_scale", 1.0)
                self._ticks_left = max(1, int(random.randint(lo, hi) * scale))

    def _transition_next(self):
        cfg = _STATE_CFG[self._state]
        if not cfg["next"]:
            self._enter_state(State.LIE)
            return
        # Skip EASTER if neither GIFs nor fallback sprites exist
        if self._state == State.RUN_AWAY:
            easter_dir = os.path.join(resource_path("assets/sprites"), "easter")
            has_gifs = os.path.isdir(easter_dir) and any(
                f.lower().endswith(".gif") for f in os.listdir(easter_dir)
            )
            if not has_gifs and not self._sprites.get(State.EASTER):
                self._enter_state(State.CAT_RETURN)
                return

        states, weights = zip(*cfg["next"])

        # For LIE, allow config to override individual next-state weights
        if self._state == State.LIE:
            weights_cfg = self._cfg.get("idle_next_weights", {})
            if weights_cfg:
                key_map = {
                    "lie":   State.LIE,
                    "walk":  State.LIE_TO_STAND,
                    "lick":  State.LIE_TO_SIT,
                    "chill": State.CHILL,
                }
                weights = tuple(
                    next((weights_cfg[k] for k, s in key_map.items() if s == st), w)
                    for st, w in zip(states, weights)
                )

        self._enter_state(random.choices(states, weights=weights, k=1)[0])

    # ── timers & animation ─────────────────────────────────────────────────

    def _start_timers(self):
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_animation)
        initial_ms = _STATE_CFG[self._state].get("anim_ms", self._cfg.get("animation_ms", 180))
        self._anim_timer.start(initial_ms)

        self._move_timer = QTimer(self)
        self._move_timer.timeout.connect(self._tick_move)
        self._move_timer.start(self._cfg.get("move_ms", 50))

    def _start_easter_gif(self):
        easter_dir = os.path.join(resource_path("assets/sprites"), "easter")
        gifs = []
        if os.path.isdir(easter_dir):
            gifs = [f for f in os.listdir(easter_dir) if f.lower().endswith(".gif")]

        if not gifs:
            # No GIFs — fall back to sprite frames + tick timer
            cfg = _STATE_CFG[State.EASTER]
            self._ticks_left = random.randint(cfg.get("min", 100), cfg.get("max", 200))
            return

        candidates = [g for g in gifs if g != self._last_easter_gif] if len(gifs) > 1 else gifs
        chosen = random.choice(candidates)
        self._last_easter_gif = chosen
        path = os.path.join(easter_dir, chosen)
        movie = QMovie(path)

        if not movie.isValid():
            cfg = _STATE_CFG[State.EASTER]
            self._ticks_left = random.randint(cfg.get("min", 100), cfg.get("max", 200))
            return

        self._easter_loops      = 0
        self._easter_frame_prev = -1
        self._ticks_left        = 10 ** 7  # block tick-based exit while GIF runs

        self._easter_movie = movie
        self._easter_movie.frameChanged.connect(self._on_easter_frame)
        self._easter_movie.start()

        # Safety timeout: some GIFs have 1 frame and never loop — force exit after 8 s
        self._easter_safety = QTimer(self)
        self._easter_safety.setSingleShot(True)
        self._easter_safety.timeout.connect(self._end_easter)
        self._easter_safety.start(8_000)

    def _end_easter(self):
        if self._easter_movie is not None:
            self._easter_movie.stop()
            try:
                self._easter_movie.frameChanged.disconnect(self._on_easter_frame)
            except Exception:
                pass
            self._easter_movie = None
        if self._state == State.EASTER:
            self._transition_next()

    def _on_easter_frame(self, frame_num: int):
        if self._easter_movie is None:
            return

        # Scale frame to easter_max_size (matches normalized cat sprite footprint)
        easter_size = self._cfg.get("easter_max_size", self._cfg.get("sprite_size", 200))
        self._current_pixmap = self._easter_movie.currentPixmap().scaled(
            easter_size, easter_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.update()

        # Detect loop boundary: frame index resets to 0 after advancing
        if frame_num == 0 and self._easter_frame_prev > 0:
            self._easter_loops += 1
            if self._easter_loops >= 2:
                if hasattr(self, "_easter_safety"):
                    self._easter_safety.stop()
                self._end_easter()
                return

        self._easter_frame_prev = frame_num

    def _tick_animation(self):
        if self._is_dragging:
            return
        if self._easter_movie is not None:
            return  # GIF drives its own frame updates via frameChanged signal
        frames = self._sprites[self._state]
        cfg    = _STATE_CFG[self._state]

        if cfg["play_once"]:
            if self._frame_idx >= len(frames) - 1:
                self._transition_next()
                return
            self._frame_idx += 1
        else:
            self._frame_idx = (self._frame_idx + 1) % len(frames)

        px = frames[self._frame_idx]
        self._current_pixmap = self._transform(px)
        self.update()

    def _transform(self, px: QPixmap) -> QPixmap:
        if self._facing_right:
            return px  # no transform needed — avoid any sub-pixel jitter
        t = QTransform()
        t.translate(px.width() / 2, px.height() / 2)
        t.scale(-1.0, 1.0)
        t.translate(-px.width() / 2, -px.height() / 2)
        return px.transformed(t, Qt.TransformationMode.FastTransformation)

    # ── movement ───────────────────────────────────────────────────────────

    def _tick_move(self):
        if self._is_dragging:
            return
        cfg = _STATE_CFG[self._state]

        if self._state == State.RUN_AWAY:
            self._step_run_away()
        elif self._state == State.CAT_RETURN:
            self._step_cat_return()
        elif cfg["moves"]:  # WALK
            self._step_walk()
        elif not cfg["play_once"]:
            if self._ticks_left > 0:
                self._ticks_left -= 1
            else:
                self._transition_next()
        # play_once states: animation tick handles the transition

    def _start_walk_segment(self):
        screen = QApplication.primaryScreen().availableGeometry()
        screen_cx = screen.width() / 2
        # Always walk toward screen center; return logic handles walking back home
        self._walk_dir     = 1 if self._pos_x < screen_cx else -1
        self._facing_right = self._walk_dir > 0
        self._walk_primary_facing = self._facing_right  # saved to restore when lying down
        self._frame_idx    = 0
        speed = self._cfg.get("walk_speed", 1.5)
        dist  = abs(screen_cx - self._pos_x)
        self._walk_ticks_left = max(1, int(dist / speed))

    def _step_walk(self):
        if self._walk_ticks_left <= 0:
            dist_to_home     = abs(self._pos_x - self._home_x)
            return_threshold = self._cfg.get("return_threshold", 50)
            if dist_to_home > return_threshold:
                # Turn toward home before lying down
                self._walk_dir        = -1 if self._pos_x > self._home_x else 1
                self._facing_right    = self._walk_dir > 0
                speed = self._cfg.get("walk_speed", 1.5)
                self._walk_ticks_left = max(1, int(dist_to_home / speed))
            else:
                # Restore facing direction from walk start so cat lies facing the right way
                self._facing_right = self._walk_primary_facing
                self._transition_next()  # → STAND_TO_LIE → LIE
            return

        speed = self._cfg.get("walk_speed", 1.5)
        self._pos_x = self._pos_x + self._walk_dir * speed
        self._walk_ticks_left -= 1
        self.move(int(self._pos_x), int(self._home_y))

    def _step_run_away(self):
        accel     = self._cfg.get("run_accel",     0.5)
        max_speed = self._cfg.get("run_max_speed", 12.0)
        self._run_speed = min(self._run_speed + accel, max_speed)
        self._pos_x += self._run_speed
        self.move(int(self._pos_x), int(self._home_y))

        size   = self._cfg.get("sprite_size", 150)
        screen = QApplication.primaryScreen().availableGeometry()
        if self._pos_x > screen.width() + size:
            self._transition_next()

    def _step_cat_return(self):
        speed = self._cfg.get("return_speed", self._cfg.get("walk_speed", 1.5) * 2)
        self._pos_x -= speed  # walk left, back toward home
        self.move(int(self._pos_x), int(self._home_y))

        if self._pos_x <= self._home_x:
            self._pos_x = self._home_x
            self._transition_next()  # → LIE

    # ── interaction ────────────────────────────────────────────────────────

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
            self._handle_click()
        else:
            self._home_x = self._pos_x
            self._home_y = self._pos_y
            self._enter_state(State.LIE, quick=True)
        self._is_dragging = False
        if hasattr(self, "_drag_offset"):
            del self._drag_offset

    def _handle_click(self):
        if self._state in (State.LIE, State.LICK):
            self._click_timer.start()
            self._click_count += 1
            if self._state == State.LIE and self._click_count >= 3:
                self._click_count = 0
                self._click_timer.stop()
                self._enter_state(State.LIE_TO_SIT)
                return
            elif self._state == State.LICK and self._click_count >= 3:
                self._click_count = 0
                self._click_timer.stop()
                self._enter_state(State.RUN_AWAY)
                return
        self._show_dialogue()

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
