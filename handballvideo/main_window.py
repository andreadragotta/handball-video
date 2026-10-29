"""Finestra principale dell'editor."""
from __future__ import annotations

import copy
import os
import time
from typing import Optional

from PySide6.QtCore import QObject, QSettings, QSize, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QShortcut, QValidator
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressDialog, QPushButton, QSlider, QSplitter, QStackedWidget, QStyle, QToolButton,
    QVBoxLayout, QWidget,
)

from . import __app_name__
from .exporter import ExportCancelled, Exporter, probe, safe_filename
from .icons import ICON_COLOR, icon
from .model import (PROJECT_EXT, TOOL_LABELS, Annotation, Clip, Cover, Item, Project,
                    fmt_time, item_name, parse_time)
from .timeline import Timeline
from .video_view import VideoView

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".wmv", ".webm")
VIDEO_FILTER = f"Video ({' '.join('*' + e for e in VIDEO_EXTS)});;Tutti i file (*)"
PROJECT_FILTER = f"Progetto analisi (*{PROJECT_EXT})"
EMPTY_HINT = "Trascina qui il video della partita\noppure usa «Apri video»"

TOOLS = [
    (None, "Puntatore"),
    ("arrow", "Freccia"),
    ("pass", "Passaggio"),
    ("line", "Linea"),
    ("rect", "Riquadro"),
    ("ellipse", "Cerchio"),
    ("text", "Testo"),
]
PRESET_COLORS = ["#FFD400", "#FF3B30", "#FFFFFF", "#00C8FF", "#34C759", "#111111"]
WIDTHS = [("Sottile", 0.004), ("Medio", 0.007), ("Spesso", 0.011)]
DEFAULT_ANNOTATION_SECONDS = 4.0

# Azioni rapide: un clic crea una clip attorno alla posizione attuale.
TAGS = [
    ("Attacco", "#2d6cdf"),
    ("Difesa", "#8e44ad"),
    ("Contropiede", "#e67e22"),
    ("Gol", "#27ae60"),
    ("Parata", "#16a085"),
    ("Tiro sbagliato", "#c0392b"),
    ("Perdita palla", "#d35400"),
    ("7 metri", "#b7950b"),
    ("2 minuti", "#7f8c8d"),
]
TAG_COLORS = dict(TAGS)
TAG_BEFORE = 8.0
TAG_AFTER = 2.0

HISTORY_LIMIT = 200
COALESCE_SECONDS = 1.0


def icon_button(text: str, icon_name: str, tooltip: str = "") -> QPushButton:
    b = QPushButton(icon(icon_name), text)
    if tooltip:
        b.setToolTip(tooltip)
    return b


class TimeSpin(QDoubleSpinBox):
    """Campo tempo che mostra/accetta m:ss.cc (si può scrivere es. 12:34)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDecimals(2)
        self.setRange(0, 24 * 3600)
        self.setSingleStep(0.1)
        self.setKeyboardTracking(False)

    def textFromValue(self, value: float) -> str:
        return fmt_time(value, precise=True)

    def valueFromText(self, text: str) -> float:
        v = parse_time(text)
        return self.value() if v is None else v

    def validate(self, text: str, pos: int):
        state = QValidator.State.Acceptable if parse_time(text) is not None else QValidator.State.Intermediate
        return state, text, pos

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)


class TimeField(QLineEdit):
    """Campo testo per un tempo: seleziona tutto al clic, Invio conferma."""

    def __init__(self, placeholder: str = "--:--", parent=None) -> None:
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedWidth(92)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)

    def value(self) -> Optional[float]:
        return parse_time(self.text())

    def flash_error(self) -> None:
        self.setStyleSheet("border:1px solid #ff453a;")
        QTimer.singleShot(1200, lambda: self.setStyleSheet(""))


class SeekSlider(QSlider):
    """Slider che salta al punto cliccato."""

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                   int(event.position().x()), self.width())
            self.setValue(value)
            self.sliderMoved.emit(value)
        super().mousePressEvent(event)


class ColorButton(QPushButton):
    changed = Signal(str)

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(60)
        self.clicked.connect(self._pick)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        self.color = color
        self.setStyleSheet(f"background:{color}; border:1px solid #888; border-radius:4px; min-height:22px;")

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self.color), self, "Scegli colore")
        if c.isValid():
            self.set_color(c.name())
            self.changed.emit(c.name())


class ExportWorker(QObject):
    progress = Signal(float)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, project: Project, items: list[Item], target: str, separate: bool) -> None:
        super().__init__()
        self.project, self.items, self.target, self.separate = project, items, target, separate
        self.cancelled = False

    def run(self) -> None:
        try:
            exp = Exporter(self.project, progress=self.progress.emit, is_cancelled=lambda: self.cancelled)
            if self.separate:
                exp.export_separately(self.items, self.target)
            else:
                exp.export_combined(self.items, self.target)
            self.done.emit(self.target)
        except ExportCancelled:
            self.failed.emit("")
        except Exception as e:  # noqa: BLE001 - mostriamo l'errore all'utente
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.resize(1440, 900)
        self.settings = QSettings("HandballVideo", "Editor")
        self.project = Project()
        self.project_path: Optional[str] = None
        self.dirty = False
        self.fps = 25.0
        self.mark_in: Optional[float] = None
        self.mark_out: Optional[float] = None
        self._prev_pos = 0.0
        self._filling = False
        self._export = None
        self._history: list[dict] = []
        self._history_index = -1
        self._coalesce_key: Optional[str] = None
        self._coalesce_time = 0.0

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.view = VideoView()
        self.view.setAcceptDrops(False)
        self.view.overlay.placeholder = EMPTY_HINT
        self.player.setVideoOutput(self.view.video_item)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_player_error)
        self.view.annotation_drawn.connect(self._on_annotation_drawn)
        self.view.annotation_selected.connect(self._on_view_annotation_selected)
        self.view.annotation_edit_finished.connect(self._on_view_annotation_moved)
        self.view.annotation_double_clicked.connect(self._on_view_annotation_double_clicked)

        self.timeline = Timeline()
        self.timeline.tag_colors = TAG_COLORS
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.clip_clicked.connect(self._select_item)
        self.timeline.clip_trimmed.connect(self._on_clip_trimmed)
        self.timeline.trim_finished.connect(lambda _id: self._mark_dirty())

        self._build_actions()
        self._build_ui()
        self._build_shortcuts()
        self.setAcceptDrops(True)
        self._reset_history()
        self._refresh_title()
        self._on_selection()
        self._restore_settings()

    # ================================================================ UI

    def _build_actions(self) -> None:
        def act(text, slot, shortcut=None, icon_name=None):
            a = QAction(icon(icon_name), text, self) if icon_name else QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            return a

        self.act_open_video = act("Apri video…", self.open_video, icon_name="open")
        self.act_new = act("Nuovo progetto", self.new_project, QKeySequence.StandardKey.New)
        self.act_open = act("Apri progetto…", self.open_project, QKeySequence.StandardKey.Open)
        self.act_save = act("Salva", self.save_project, QKeySequence.StandardKey.Save, "save")
        self.act_save_as = act("Salva con nome…", self.save_project_as, QKeySequence.StandardKey.SaveAs)
        self.act_undo = act("Annulla", self.undo, QKeySequence.StandardKey.Undo, "undo")
        self.act_redo = act("Ripeti", self.redo, QKeySequence.StandardKey.Redo, "redo")
        self.act_export_sel = act("Esporta elemento selezionato…", self.export_selected)
        self.act_export_all = act("Esporta tutto in un video…", self.export_all, icon_name="export")
        self.act_export_sep = act("Esporta ogni elemento in file separati…", self.export_separate)
        self.act_timeline = act("Mostra timeline", self.toggle_timeline, "T", "timeline")
        self.act_timeline.setCheckable(True)

        m = self.menuBar().addMenu("File")
        for a in (self.act_new, self.act_open, self.act_open_video, None, self.act_save, self.act_save_as,
                  None, act("Esci", self.close, QKeySequence.StandardKey.Quit)):
            m.addSeparator() if a is None else m.addAction(a)
        m = self.menuBar().addMenu("Modifica")
        m.addAction(self.act_undo)
        m.addAction(self.act_redo)
        m = self.menuBar().addMenu("Vista")
        m.addAction(self.act_timeline)
        m = self.menuBar().addMenu("Esporta")
        for a in (self.act_export_sel, self.act_export_all, self.act_export_sep):
            m.addAction(a)
        m = self.menuBar().addMenu("Aiuto")
        m.addAction(act("Scorciatoie da tastiera", self.show_shortcuts))

        tb = self.addToolBar("Principale")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setIconSize(QSize(18, 18))
        tb.addAction(self.act_open_video)
        tb.addAction(self.act_save)
        tb.addSeparator()
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)
        tb.addSeparator()
        tb.addAction(self.act_timeline)
        tb.addSeparator()
        tb.addAction(self.act_export_all)

    def _build_ui(self) -> None:
        # ---------- colonna sinistra: video, strumenti, trasporto, timeline, marcatura
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 4, 8)
        lv.addWidget(self.view, 1)
        lv.addLayout(self._build_tools_row())
        lv.addLayout(self._build_transport_row())

        self.timeline_box = QWidget()
        tl = QVBoxLayout(self.timeline_box)
        tl.setContentsMargins(0, 2, 0, 2)
        head = QHBoxLayout()
        hint = QLabel("Rotellina: zoom  ·  Shift + rotellina: scorri  ·  trascina i bordi di una clip per regolarla")
        hint.setStyleSheet("color:#8a8c93; font-size:11px")
        head.addWidget(hint, 1)
        for name, tip, slot in (("zoom-out", "Riduci zoom", lambda: self.timeline.zoom(1.6)),
                                ("zoom-in", "Aumenta zoom", lambda: self.timeline.zoom(0.6)),
                                ("fit", "Mostra tutta la partita", self.timeline.fit)):
            b = QToolButton()
            b.setIcon(icon(name))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            head.addWidget(b)
        tl.addLayout(head)
        tl.addWidget(self.timeline)
        lv.addWidget(self.timeline_box)

        lv.addLayout(self._build_mark_row())
        lv.addLayout(self._build_tags_row())

        # ---------- colonna destra: sequenza + dettagli
        right = QWidget()
        right.setMinimumWidth(360)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 8, 8, 8)
        rv.addWidget(QLabel("<b>Sequenza</b> — trascina per riordinare"))
        self.seq = QListWidget()
        self.seq.setIconSize(QSize(18, 18))
        self.seq.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.seq.currentItemChanged.connect(lambda *_: self._on_selection(seek=True))
        self.seq.model().rowsMoved.connect(lambda *_: QTimer.singleShot(0, self._sync_order))
        rv.addWidget(self.seq, 1)
        row = QHBoxLayout()
        for text, name, tip, slot in (("Copertina", "cover", "Aggiungi copertina prima dell'elemento selezionato",
                                       self.add_cover),
                                      ("", "up", "Sposta su", lambda: self.move_item(-1)),
                                      ("", "down", "Sposta giù", lambda: self.move_item(1)),
                                      ("Elimina", "delete", "", self.delete_item)):
            b = icon_button(text, name, tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        rv.addLayout(row)

        self.details = QStackedWidget()
        empty = QLabel("Seleziona una clip o una copertina\nper modificarla.")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setStyleSheet("color:#888")
        self.details.addWidget(empty)
        self.details.addWidget(self._build_clip_panel())
        self.details.addWidget(self._build_cover_panel())
        rv.addWidget(self.details, 1)

        self.splitter = QSplitter()
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setSizes([1060, 380])
        self.setCentralWidget(self.splitter)

    def _build_tools_row(self) -> QHBoxLayout:
        tools = QHBoxLayout()
        tools.addWidget(QLabel("<b>Disegna:</b>"))
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        for i, (key, label) in enumerate(TOOLS):
            b = QToolButton()
            b.setText(label)
            b.setIcon(icon(key or "pointer"))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setCheckable(True)
            b.setProperty("tool", key)
            b.setToolTip("Seleziona, sposta e ridimensiona i disegni" if key is None
                         else f"Disegna: {label.lower()}")
            self.tool_group.addButton(b, i)
            tools.addWidget(b)
            if key is None:
                b.setChecked(True)
        self.tool_group.buttonClicked.connect(self._on_tool_clicked)
        tools.addSpacing(12)
        self.swatch_group = QButtonGroup(self)
        for c in PRESET_COLORS:
            b = QToolButton()
            b.setCheckable(True)
            b.setFixedSize(22, 22)
            b.setProperty("color", c)
            b.setStyleSheet(f"QToolButton{{background:{c}; border:1px solid #666; border-radius:11px}}"
                            f"QToolButton:checked{{border:3px solid #4da3ff}}")
            self.swatch_group.addButton(b)
            tools.addWidget(b)
            if c == self.view.color:
                b.setChecked(True)
        self.swatch_group.buttonClicked.connect(lambda b: self._set_draw_color(b.property("color")))
        custom = QToolButton()
        custom.setIcon(icon("palette"))
        custom.setToolTip("Altro colore")
        custom.clicked.connect(self._pick_custom_color)
        tools.addWidget(custom)
        self.width_combo = QComboBox()
        for label, w in WIDTHS:
            self.width_combo.addItem(label, w)
        self.width_combo.setCurrentIndex(1)
        self.width_combo.setToolTip("Spessore")
        self.width_combo.currentIndexChanged.connect(self._set_draw_width)
        tools.addWidget(self.width_combo)
        tools.addStretch()
        return tools

    def _build_transport_row(self) -> QHBoxLayout:
        transport = QHBoxLayout()
        self.btn_play = icon_button("", "play", "Play / pausa (Spazio)")
        self.btn_play.setFixedWidth(48)
        self.btn_play.clicked.connect(self.toggle_play)
        transport.addWidget(self.btn_play)
        for text, name, tip, slot in (
                ("5s", "back", "Indietro 5 secondi (J)", lambda: self.seek_rel(-5)),
                ("", "frame-back", "Fotogramma precedente (←)", lambda: self.step_frame(-1)),
                ("", "frame-forward", "Fotogramma successivo (→)", lambda: self.step_frame(1)),
                ("5s", "forward", "Avanti 5 secondi (L)", lambda: self.seek_rel(5))):
            b = icon_button(text, name, tip)
            b.setMinimumWidth(44)
            b.clicked.connect(slot)
            transport.addWidget(b)
        self.slider = SeekSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(lambda v: self.player.setPosition(v))
        transport.addWidget(self.slider, 1)
        self.time_field = TimeField("0:00.00")
        self.time_field.setToolTip("Scrivi un tempo (es. 23:45) e premi Invio per andarci")
        self.time_field.returnPressed.connect(self._goto_typed_time)
        transport.addWidget(self.time_field)
        self.duration_label = QLabel("/ 0:00")
        self.duration_label.setMinimumWidth(60)
        transport.addWidget(self.duration_label)
        self.speed = QComboBox()
        for r in (0.25, 0.5, 1.0, 1.5, 2.0):
            self.speed.addItem(f"{r:g}×", r)
        self.speed.setCurrentIndex(2)
        self.speed.setToolTip("Velocità di riproduzione")
        self.speed.currentIndexChanged.connect(lambda: self.player.setPlaybackRate(self.speed.currentData()))
        transport.addWidget(self.speed)
        return transport

    def _build_mark_row(self) -> QHBoxLayout:
        mark = QHBoxLayout()
        b = icon_button("Inizio (I)", "mark-in", "Segna l'inizio della clip alla posizione attuale")
        b.clicked.connect(self.set_mark_in)
        mark.addWidget(b)
        self.in_field = TimeField()
        self.in_field.setToolTip("Inizio clip: puoi anche scriverlo (es. 12:34)")
        self.in_field.editingFinished.connect(lambda: self._on_mark_typed("in"))
        mark.addWidget(self.in_field)
        b = icon_button("Fine (O)", "mark-out", "Segna la fine della clip alla posizione attuale")
        b.clicked.connect(self.set_mark_out)
        mark.addWidget(b)
        self.out_field = TimeField()
        self.out_field.setToolTip("Fine clip: puoi anche scriverla (es. 12:48)")
        self.out_field.editingFinished.connect(lambda: self._on_mark_typed("out"))
        mark.addWidget(self.out_field)
        self.clip_name = QLineEdit()
        self.clip_name.setPlaceholderText("Nome clip (es. Contropiede 12')")
        self.clip_name.returnPressed.connect(self.add_clip)
        mark.addWidget(self.clip_name, 1)
        b = icon_button("Aggiungi clip", "plus")
        b.setObjectName("primary")
        b.clicked.connect(self.add_clip)
        mark.addWidget(b)
        return mark

    def _build_tags_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel("<b>Azione rapida:</b>")
        label.setToolTip(f"Crea una clip da {TAG_BEFORE:g} s prima a {TAG_AFTER:g} s dopo la posizione attuale "
                         "(o tra Inizio e Fine, se segnati)")
        row.addWidget(label)
        for n, (tag, color) in enumerate(TAGS, 1):
            b = QPushButton(icon("tag", color), tag)
            b.setToolTip(f"Crea clip «{tag}» (tasto {n})")
            b.clicked.connect(lambda _=False, t=tag: self.add_tag_clip(t))
            row.addWidget(b)
        row.addStretch()
        return row

    def _build_clip_panel(self) -> QWidget:
        box = QGroupBox("Clip")
        form = QFormLayout(box)
        self.f_name = QLineEdit()
        self.f_name.textEdited.connect(self._clip_edited)
        form.addRow("Nome", self.f_name)
        self.f_start, self.f_end = TimeSpin(), TimeSpin()
        for label, spin in (("Inizio", self.f_start), ("Fine", self.f_end)):
            spin.setToolTip("Scrivi il tempo (es. 12:34.5) o usa le frecce")
            spin.valueChanged.connect(self._clip_edited)
            b = icon_button("Qui", "here", "Usa la posizione attuale del video")
            b.clicked.connect(lambda _=False, s=spin: s.setValue(self.position()))
            h = QHBoxLayout()
            h.addWidget(spin, 1)
            h.addWidget(b)
            form.addRow(label, h)
        h = QHBoxLayout()
        b = icon_button("Riproduci clip", "play")
        b.clicked.connect(self.play_clip)
        h.addWidget(b, 1)
        self.loop_clip = QCheckBox("Ripeti")
        self.loop_clip.setIcon(icon("repeat"))
        self.loop_clip.setToolTip("Riproduci la clip in continuo")
        h.addWidget(self.loop_clip)
        form.addRow(h)

        form.addRow(QLabel("<b>Disegni</b> — clicca per selezionarli anche sul video"))
        self.ann_list = QListWidget()
        self.ann_list.setMinimumHeight(110)
        self.ann_list.setMaximumHeight(190)
        self.ann_list.currentRowChanged.connect(self._on_annotation_row)
        form.addRow(self.ann_list)
        self.a_start, self.a_end = TimeSpin(), TimeSpin()
        self.a_start.setToolTip("Tempo dall'inizio della clip")
        self.a_end.setToolTip("Tempo dall'inizio della clip")
        self.a_to_end = QCheckBox("fino a fine clip")
        self.a_start.valueChanged.connect(self._annotation_edited)
        self.a_end.valueChanged.connect(self._annotation_edited)
        self.a_to_end.toggled.connect(self._annotation_edited)
        h = QHBoxLayout()
        h.addWidget(self.a_start, 1)
        b = icon_button("Qui", "here", "Usa la posizione attuale del video")
        b.clicked.connect(lambda: self.a_start.setValue(self._rel_position()))
        h.addWidget(b)
        form.addRow("Appare a", h)
        h = QHBoxLayout()
        h.addWidget(self.a_end, 1)
        b = icon_button("Qui", "here", "Usa la posizione attuale del video")
        b.clicked.connect(lambda: (self.a_to_end.setChecked(False), self.a_end.setValue(self._rel_position())))
        h.addWidget(b)
        form.addRow("Sparisce a", h)
        form.addRow("", self.a_to_end)
        h = QHBoxLayout()
        self.btn_edit_text = icon_button("Modifica testo", "edit")
        self.btn_edit_text.clicked.connect(self._edit_annotation_text)
        h.addWidget(self.btn_edit_text)
        self.btn_delete_ann = icon_button("Elimina disegno", "delete", "Elimina (Canc)")
        self.btn_delete_ann.clicked.connect(self.delete_annotation)
        h.addWidget(self.btn_delete_ann)
        form.addRow(h)
        return box

    def _build_cover_panel(self) -> QWidget:
        box = QGroupBox("Copertina")
        form = QFormLayout(box)
        self.c_title = QLineEdit()
        self.c_title.textEdited.connect(self._cover_edited)
        form.addRow("Titolo", self.c_title)
        self.c_sub = QPlainTextEdit()
        self.c_sub.setMaximumHeight(90)
        self.c_sub.textChanged.connect(self._cover_edited)
        form.addRow("Sottotitolo", self.c_sub)
        self.c_bg = ColorButton("#0B3D91")
        self.c_bg.changed.connect(self._cover_edited)
        form.addRow("Sfondo", self.c_bg)
        self.c_fg = ColorButton("#FFFFFF")
        self.c_fg.changed.connect(self._cover_edited)
        form.addRow("Testo", self.c_fg)
        self.c_dur = QDoubleSpinBox()
        self.c_dur.setRange(0.5, 60)
        self.c_dur.setSingleStep(0.5)
        self.c_dur.setSuffix(" s")
        self.c_dur.valueChanged.connect(self._cover_edited)
        form.addRow("Durata", self.c_dur)
        return box

    def _build_shortcuts(self) -> None:
        keys = [("Space", self.toggle_play), ("Left", lambda: self.step_frame(-1)),
                ("Right", lambda: self.step_frame(1)), ("J", lambda: self.seek_rel(-5)),
                ("L", lambda: self.seek_rel(5)), ("Shift+Left", lambda: self.seek_rel(-1)),
                ("Shift+Right", lambda: self.seek_rel(1)), ("I", self.set_mark_in),
                ("O", self.set_mark_out), ("Delete", self._delete_key), ("Backspace", self._delete_key),
                ("Escape", self._escape)]
        keys += [(str(n), lambda t=tag: self.add_tag_clip(t)) for n, (tag, _) in enumerate(TAGS, 1)]
        for key, slot in keys:
            QShortcut(QKeySequence(key), self, slot)

    def show_shortcuts(self) -> None:
        QMessageBox.information(self, "Scorciatoie", (
            "Spazio — play / pausa\n"
            "← / →  — fotogramma precedente / successivo\n"
            "Shift + ← / →  — indietro / avanti 1 secondo\n"
            "J / L  — indietro / avanti 5 secondi\n"
            "I / O  — segna inizio / fine della clip\n"
            "1 … 9  — azioni rapide (Attacco, Difesa, …)\n"
            "T — mostra / nascondi timeline\n"
            "Canc — elimina il disegno selezionato\n"
            "Ctrl/Cmd + Z — annulla  ·  Ctrl/Cmd + Shift + Z — ripeti\n"
            "Esc — torna al puntatore / deseleziona\n\n"
            "Sul video, con il Puntatore: clic su un disegno per selezionarlo,\n"
            "trascinalo per spostarlo, usa i quadratini per ridimensionarlo,\n"
            "doppio clic su un testo per modificarlo."))

    # ========================================================= impostazioni

    def _restore_settings(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        visible = self.settings.value("timeline_visible", True, type=bool)
        self.act_timeline.setChecked(visible)
        self.timeline_box.setVisible(visible)

    def _save_settings(self) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("timeline_visible", self.act_timeline.isChecked())

    def _last_dir(self) -> str:
        return self.settings.value("last_dir", os.path.expanduser("~"), type=str)

    def _remember_dir(self, path: str) -> None:
        self.settings.setValue("last_dir", path if os.path.isdir(path) else os.path.dirname(path))

    def toggle_timeline(self) -> None:
        self.timeline_box.setVisible(self.act_timeline.isChecked())

    # =========================================================== helpers

    def position(self) -> float:
        return self.player.position() / 1000.0

    def current_item(self) -> Optional[Item]:
        it = self.seq.currentItem()
        return self.project.find(it.data(Qt.ItemDataRole.UserRole)) if it else None

    def current_clip(self) -> Optional[Clip]:
        item = self.current_item()
        return item if isinstance(item, Clip) else None

    def _rel_position(self) -> float:
        clip = self.current_clip()
        return max(0.0, self.position() - clip.start) if clip else 0.0

    def _refresh_title(self) -> None:
        name = os.path.basename(self.project_path) if self.project_path else "Nuovo progetto"
        self.setWindowTitle(f"{'• ' if self.dirty else ''}{name} — {__app_name__}")

    def _on_tool_clicked(self, button) -> None:
        tool = button.property("tool")
        self.view.set_tool(tool)
        if tool is not None:
            self.view.select_annotation(None)

    def _escape(self) -> None:
        self.tool_group.button(0).setChecked(True)
        self.view.set_tool(None)
        if self.view.overlay.selected_id:
            self.view.select_annotation(None)
            self._select_annotation_row(None)
        focus = QApplication.focusWidget()
        if isinstance(focus, QLineEdit):
            focus.clearFocus()

    def _set_draw_color(self, color: str) -> None:
        self.view.color = color
        # Se c'è un disegno selezionato, il colore si applica anche a quello.
        a = self._current_annotation()
        if a and self.view.overlay.selected_id == a.id:
            a.color = color
            self._after_annotation_change(a)

    def _set_draw_width(self) -> None:
        self.view.width_rel = self.width_combo.currentData()
        a = self._current_annotation()
        if a and self.view.overlay.selected_id == a.id and a.kind != "text":
            a.width = self.view.width_rel
            self._after_annotation_change(a)

    def _pick_custom_color(self) -> None:
        c = QColorDialog.getColor(QColor(self.view.color), self, "Colore disegno")
        if c.isValid():
            checked = self.swatch_group.checkedButton()
            if checked:
                self.swatch_group.setExclusive(False)
                checked.setChecked(False)
                self.swatch_group.setExclusive(True)
            self._set_draw_color(c.name())

    def _select_item(self, item_id: str) -> None:
        for i in range(self.seq.count()):
            if self.seq.item(i).data(Qt.ItemDataRole.UserRole) == item_id:
                if self.seq.currentRow() != i:
                    self.seq.setCurrentRow(i)
                return

    def _sync_timeline(self) -> None:
        tl = self.timeline
        tl.clips = [i for i in self.project.items if isinstance(i, Clip)]
        item = self.current_item()
        tl.selected_id = item.id if item else None
        tl.mark_in, tl.mark_out = self.mark_in, self.mark_out
        tl.update()

    # ===================================================== annulla / ripeti

    def _reset_history(self) -> None:
        self._history = [self.project.to_dict()]
        self._history_index = 0
        self._coalesce_key = None
        self._update_undo_actions()

    def _mark_dirty(self, coalesce: Optional[str] = None) -> None:
        """Registra lo stato dopo una modifica (per annulla/ripeti).

        Modifiche ravvicinate con la stessa chiave (es. frecce di un campo
        tempo) vengono unite in un solo passo di annullamento.
        """
        state = self.project.to_dict()
        now = time.monotonic()
        at_top = self._history_index == len(self._history) - 1
        if coalesce and coalesce == self._coalesce_key and at_top and now - self._coalesce_time < COALESCE_SECONDS:
            self._history[self._history_index] = state
        else:
            del self._history[self._history_index + 1:]
            self._history.append(state)
            if len(self._history) > HISTORY_LIMIT:
                self._history.pop(0)
            self._history_index = len(self._history) - 1
        self._coalesce_key, self._coalesce_time = coalesce, now
        self.dirty = True
        self._refresh_title()
        self._update_undo_actions()

    def _update_undo_actions(self) -> None:
        self.act_undo.setEnabled(self._history_index > 0)
        self.act_redo.setEnabled(self._history_index < len(self._history) - 1)

    def undo(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self._restore_state(self._history[self._history_index])

    def redo(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._restore_state(self._history[self._history_index])

    def _restore_state(self, state: dict) -> None:
        item = self.current_item()
        ann = self._current_annotation()
        video = self.project.video_path
        self.project = Project.from_dict(state)
        self.project.video_path = video  # annulla non cambia il video aperto
        self._coalesce_key = None
        self._refresh_sequence(select_id=item.id if item else None, keep_selection=True)
        if ann:
            self._refresh_annotations(select_id=ann.id)
        self.dirty = True
        self._refresh_title()
        self._update_undo_actions()

    # ============================================================ player

    def load_video(self, path: str) -> None:
        try:
            info = probe(path)
            self.fps = info.fps or 25.0
            self.view.set_frame_size(info.width, info.height)
        except Exception:
            self.fps = 25.0
        self.project.video_path = path
        self.view.overlay.placeholder = None
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.pause()
        self._remember_dir(path)

    def toggle_play(self) -> None:
        if not self.project.video_path:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek(self, seconds: float) -> None:
        self.player.setPosition(int(max(0.0, seconds) * 1000))

    def seek_rel(self, delta: float) -> None:
        self.seek(self.position() + delta)

    def step_frame(self, n: int) -> None:
        self.player.pause()
        self.seek(self.position() + n / self.fps)

    def play_clip(self) -> None:
        clip = self.current_clip()
        if clip:
            self.seek(clip.start)
            self.player.play()

    def _goto_typed_time(self) -> None:
        t = self.time_field.value()
        if t is None:
            self.time_field.flash_error()
            return
        self.seek(t)
        self.view.setFocus()

    def _on_position(self, ms: int) -> None:
        pos = ms / 1000.0
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        if not self.time_field.hasFocus():
            self.time_field.setText(fmt_time(pos, True))
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        clip = self.current_clip()
        # Arrivati a fine clip mentre la si guarda: pausa (o ricomincia se «Ripeti»).
        if clip and playing and clip.start <= self._prev_pos < clip.end <= pos:
            if self.loop_clip.isChecked():
                self.seek(clip.start)
            else:
                self.player.pause()
                self.seek(clip.end)
        self._prev_pos = pos
        self.timeline.set_position(pos, follow=playing)
        self._update_overlay()

    def _on_duration(self, ms: int) -> None:
        self.slider.setRange(0, ms)
        self.duration_label.setText(f"/ {fmt_time(ms / 1000)}")
        self.timeline.set_duration(ms / 1000)
        self._sync_timeline()

    def _on_state(self, state) -> None:
        self.btn_play.setIcon(icon("pause" if state == QMediaPlayer.PlaybackState.PlayingState else "play"))

    def _on_player_error(self, error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError:
            QMessageBox.warning(self, "Errore video", message or "Impossibile riprodurre il video.")

    def _update_overlay(self) -> None:
        item = self.current_item()
        ov = self.view.overlay
        ov.cover = item if isinstance(item, Cover) else None
        anns: list[Annotation] = []
        if isinstance(item, Clip):
            pos = self.position()
            if item.start - 0.01 <= pos <= item.end + 0.01:
                rel = pos - item.start
                anns = [a for a in item.annotations if a.visible_at(rel, item.duration)]
        ov.annotations = anns
        ov.update()

    # ============================================================= clips

    def set_mark_in(self) -> None:
        self.mark_in = self.position()
        if self.mark_out is not None and self.mark_out <= self.mark_in:
            self.mark_out = None
        self._refresh_marks()

    def set_mark_out(self) -> None:
        pos = self.position()
        if self.mark_in is not None and pos <= self.mark_in:
            self.statusBar().showMessage("La fine deve essere dopo l'inizio", 3000)
            return
        self.mark_out = pos
        self._refresh_marks()

    def _on_mark_typed(self, which: str) -> None:
        field = self.in_field if which == "in" else self.out_field
        text = field.text().strip()
        value = parse_time(text) if text else None
        if text and value is None:
            field.flash_error()
            self._refresh_marks()
            return
        if value == getattr(self, f"mark_{which}"):
            return
        setattr(self, f"mark_{which}", value)
        self._refresh_marks()
        if value is not None:
            self.seek(value)

    def _refresh_marks(self) -> None:
        for field, value in ((self.in_field, self.mark_in), (self.out_field, self.mark_out)):
            if not field.hasFocus():
                field.setText(fmt_time(value, True) if value is not None else "")
        self._sync_timeline()

    def _ensure_video(self) -> bool:
        if self.project.video_path:
            return True
        QMessageBox.information(self, "Nessun video", "Apri prima il video della partita.")
        return False

    def add_clip(self) -> None:
        if not self._ensure_video():
            return
        start = self.mark_in
        end = self.mark_out if self.mark_out is not None else self.position()
        if start is None or end <= start:
            QMessageBox.information(self, "Segna la clip",
                                    "Porta il video all'inizio dell'azione e premi «Inizio (I)», "
                                    "poi alla fine e premi «Fine (O)».\n"
                                    "Puoi anche scrivere i tempi nei campi accanto ai pulsanti.")
            return
        n = sum(isinstance(i, Clip) for i in self.project.items) + 1
        self._append_clip(Clip(name=self.clip_name.text().strip() or f"Clip {n}", start=start, end=end))
        self.clip_name.clear()

    def add_tag_clip(self, tag: str) -> None:
        if not self._ensure_video():
            return
        pos = self.position()
        if self.mark_in is not None:
            start = self.mark_in
            end = self.mark_out if self.mark_out is not None else max(pos, start + TAG_AFTER)
        else:
            start = max(0.0, pos - TAG_BEFORE)
            end = pos + TAG_AFTER
            duration = self.player.duration() / 1000
            if duration > 0:
                end = min(end, duration)
        if end - start < 0.2:
            return
        n = sum(1 for i in self.project.items if isinstance(i, Clip) and i.tag == tag) + 1
        self._append_clip(Clip(name=f"{tag} {n}", start=start, end=end, tag=tag))

    def _append_clip(self, clip: Clip) -> None:
        self.project.items.append(clip)
        self.mark_in = self.mark_out = None
        self._refresh_marks()
        self._refresh_sequence(keep_selection=True)
        self._mark_dirty()
        self.statusBar().showMessage(
            f"Aggiunta «{clip.name}»  {fmt_time(clip.start, True)} – {fmt_time(clip.end, True)}", 4000)

    def add_cover(self) -> None:
        row = self.seq.currentRow()
        cover = Cover(title="Titolo", subtitle="")
        self.project.items.insert(max(row, 0), cover)
        self._refresh_sequence(select_id=cover.id)
        self._mark_dirty()
        self.c_title.setFocus()
        self.c_title.selectAll()

    def move_item(self, delta: int) -> None:
        row = self.seq.currentRow()
        new = row + delta
        if row < 0 or not 0 <= new < len(self.project.items):
            return
        items = self.project.items
        items[row], items[new] = items[new], items[row]
        self._refresh_sequence(select_id=items[new].id, keep_selection=True)
        self._mark_dirty()

    def delete_item(self) -> None:
        item = self.current_item()
        if not item:
            return
        self.project.items.remove(item)
        self._refresh_sequence()
        self._mark_dirty()
        self.statusBar().showMessage(f"Eliminato «{item_name(item)}» — Ctrl/Cmd+Z per annullare", 5000)

    def _delete_key(self) -> None:
        if self.seq.hasFocus():
            self.delete_item()
        elif self.view.overlay.selected_id or self.ann_list.hasFocus():
            self.delete_annotation()

    def _sync_order(self) -> None:
        ids = [self.seq.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.seq.count())]
        by_id = {i.id: i for i in self.project.items}
        if set(ids) == set(by_id) and ids != [i.id for i in self.project.items]:
            self.project.items = [by_id[i] for i in ids]
            self._mark_dirty()

    @staticmethod
    def _item_label(item: Item) -> str:
        if isinstance(item, Cover):
            return f"{item.title or 'Copertina'}   ·  copertina {item.duration:g}s"
        n = len(item.annotations)
        drawn = f"  ·  {n} disegn{'o' if n == 1 else 'i'}" if n else ""
        return f"{item.name}   ·  {fmt_time(item.start)}–{fmt_time(item.end)}{drawn}"

    @staticmethod
    def _item_icon(item: Item):
        if isinstance(item, Cover):
            return icon("cover")
        return icon("clip", TAG_COLORS.get(item.tag, ICON_COLOR))

    def _refresh_sequence(self, select_id: Optional[str] = None, keep_selection: bool = False) -> None:
        if keep_selection and select_id is None:
            cur = self.current_item()
            select_id = cur.id if cur else None
        self.seq.blockSignals(True)
        self.seq.clear()
        for item in self.project.items:
            li = QListWidgetItem(self._item_icon(item), self._item_label(item))
            li.setData(Qt.ItemDataRole.UserRole, item.id)
            self.seq.addItem(li)
            if item.id == select_id:
                self.seq.setCurrentItem(li)
        self.seq.blockSignals(False)
        self._on_selection(seek=not keep_selection)

    def _update_current_label(self) -> None:
        it, item = self.seq.currentItem(), self.current_item()
        if it and item:
            it.setText(self._item_label(item))
            it.setIcon(self._item_icon(item))

    def _on_clip_trimmed(self, clip_id: str) -> None:
        clip = self.current_clip()
        if clip and clip.id == clip_id:
            self._filling = True
            self.f_start.setValue(clip.start)
            self.f_end.setValue(clip.end)
            self._filling = False
            self._update_current_label()

    # ========================================================== dettagli

    def _on_selection(self, seek: bool = False) -> None:
        item = self.current_item()
        self._filling = True
        if isinstance(item, Clip):
            self.details.setCurrentIndex(1)
            self.f_name.setText(item.name)
            self.f_start.setValue(item.start)
            self.f_end.setValue(item.end)
            self._refresh_annotations()
            if seek:
                self.player.pause()
                self.seek(item.start)
        elif isinstance(item, Cover):
            self.details.setCurrentIndex(2)
            self.c_title.setText(item.title)
            self.c_sub.setPlainText(item.subtitle)
            self.c_bg.set_color(item.bg_color)
            self.c_fg.set_color(item.text_color)
            self.c_dur.setValue(item.duration)
            self.player.pause()
        else:
            self.details.setCurrentIndex(0)
        if not isinstance(item, Clip):
            self.view.select_annotation(None)
        self._filling = False
        self._sync_timeline()
        self._update_overlay()

    def _clip_edited(self) -> None:
        clip = self.current_clip()
        if self._filling or not clip:
            return
        clip.name = self.f_name.text()
        start, end = self.f_start.value(), self.f_end.value()
        if end > start:
            clip.start, clip.end = start, end
        self._update_current_label()
        self._sync_timeline()
        self._update_overlay()
        self._mark_dirty(coalesce=f"clip:{clip.id}")

    def _cover_edited(self) -> None:
        item = self.current_item()
        if self._filling or not isinstance(item, Cover):
            return
        item.title = self.c_title.text()
        item.subtitle = self.c_sub.toPlainText()
        item.bg_color = self.c_bg.color
        item.text_color = self.c_fg.color
        item.duration = self.c_dur.value()
        self._update_current_label()
        self._update_overlay()
        self._mark_dirty(coalesce=f"cover:{item.id}")

    # ======================================================= annotazioni

    def _on_annotation_drawn(self, ann: Annotation) -> None:
        clip = self.current_clip()
        pos = self.position()
        if not clip or not (clip.start - 0.01 <= pos <= clip.end + 0.01):
            QMessageBox.information(self, "Seleziona una clip",
                                    "Per disegnare seleziona una clip nella Sequenza (o sulla timeline) "
                                    "e porta il video in un punto della clip.")
            return
        if ann.kind == "text":
            text, ok = QInputDialog.getMultiLineText(self, "Testo", "Testo del riquadro:")
            if not ok or not text.strip():
                return
            ann.text = text.strip()
        ann.start = max(0.0, pos - clip.start)
        ann.end = min(ann.start + DEFAULT_ANNOTATION_SECONDS, clip.duration)
        clip.annotations.append(ann)
        self._refresh_annotations(select_id=ann.id)
        self._update_current_label()
        self._sync_timeline()
        self._update_overlay()
        self._mark_dirty()

    def _annotation_label(self, a: Annotation) -> str:
        end = "fine clip" if a.end is None else fmt_time(a.end, True)
        label = f"{TOOL_LABELS.get(a.kind, a.kind)}   {fmt_time(a.start, True)} → {end}"
        if a.text:
            label += f"   «{a.text[:24]}»"
        return label

    def _refresh_annotations(self, select_id: Optional[str] = None) -> None:
        clip = self.current_clip()
        self.ann_list.blockSignals(True)
        self.ann_list.clear()
        if clip:
            for a in clip.annotations:
                li = QListWidgetItem(icon(a.kind, a.color if a.color.upper() != "#111111" else ICON_COLOR),
                                     self._annotation_label(a))
                self.ann_list.addItem(li)
                if a.id == select_id:
                    self.ann_list.setCurrentItem(li)
        self.ann_list.blockSignals(False)
        self._fill_annotation()
        a = self._current_annotation()
        self.view.select_annotation(a.id if a else None)

    def _current_annotation(self) -> Optional[Annotation]:
        clip, row = self.current_clip(), self.ann_list.currentRow()
        return clip.annotations[row] if clip and 0 <= row < len(clip.annotations) else None

    def _select_annotation_row(self, ann_id: Optional[str]) -> None:
        clip = self.current_clip()
        row = -1
        if clip and ann_id:
            row = next((i for i, a in enumerate(clip.annotations) if a.id == ann_id), -1)
        self.ann_list.blockSignals(True)
        self.ann_list.setCurrentRow(row)
        self.ann_list.blockSignals(False)
        self._fill_annotation()

    def _on_annotation_row(self, _row: int) -> None:
        self._fill_annotation()
        a, clip = self._current_annotation(), self.current_clip()
        self.view.select_annotation(a.id if a else None)
        # Porta il video dove il disegno è visibile.
        if a and clip and not a.visible_at(self.position() - clip.start, clip.duration):
            self.player.pause()
            self.seek(clip.start + a.start)

    def _on_view_annotation_selected(self, ann_id: str) -> None:
        self._select_annotation_row(ann_id or None)

    def _on_view_annotation_moved(self) -> None:
        self._mark_dirty()

    def _on_view_annotation_double_clicked(self, ann_id: str) -> None:
        self._select_annotation_row(ann_id)
        self._edit_annotation_text()

    def _fill_annotation(self) -> None:
        a, clip = self._current_annotation(), self.current_clip()
        for w in (self.a_start, self.a_end, self.a_to_end, self.btn_edit_text, self.btn_delete_ann):
            w.setEnabled(a is not None)
        if not a or not clip:
            return
        prev, self._filling = self._filling, True
        self.a_start.setValue(a.start)
        self.a_end.setValue(a.end_in(clip.duration))
        self.a_to_end.setChecked(a.end is None)
        self.a_end.setEnabled(a.end is not None)
        self.btn_edit_text.setEnabled(a.kind == "text")
        self._filling = prev

    def _after_annotation_change(self, a: Annotation, coalesce: Optional[str] = None) -> None:
        li = self.ann_list.currentItem()
        if li:
            li.setText(self._annotation_label(a))
            li.setIcon(icon(a.kind, a.color if a.color.upper() != "#111111" else ICON_COLOR))
        self._sync_timeline()
        self._update_overlay()
        self._mark_dirty(coalesce)

    def _annotation_edited(self) -> None:
        a = self._current_annotation()
        if self._filling or not a:
            return
        a.start = self.a_start.value()
        a.end = None if self.a_to_end.isChecked() else max(self.a_end.value(), a.start + 0.1)
        self.a_end.setEnabled(not self.a_to_end.isChecked())
        self._after_annotation_change(a, coalesce=f"ann:{a.id}")

    def _edit_annotation_text(self) -> None:
        a = self._current_annotation()
        if not a or a.kind != "text":
            return
        text, ok = QInputDialog.getMultiLineText(self, "Testo", "Testo del riquadro:", a.text)
        if ok and text.strip():
            a.text = text.strip()
            self._after_annotation_change(a)

    def delete_annotation(self) -> None:
        clip, a = self.current_clip(), self._current_annotation()
        if clip and a:
            clip.annotations.remove(a)
            self._refresh_annotations()
            self._update_current_label()
            self._sync_timeline()
            self._update_overlay()
            self._mark_dirty()

    # ========================================================== progetto

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        r = QMessageBox.question(
            self, "Modifiche non salvate", "Vuoi salvare le modifiche al progetto?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save_project()
        return r == QMessageBox.StandardButton.Discard

    def _set_project(self, project: Project, path: Optional[str]) -> None:
        self.project, self.project_path, self.dirty = project, path, False
        self.mark_in = self.mark_out = None
        self._refresh_marks()
        self._refresh_sequence()
        self._reset_history()
        self._refresh_title()

    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.player.stop()
        self.player.setSource(QUrl())
        self.view.overlay.placeholder = EMPTY_HINT
        self.timeline.set_duration(0)
        self._set_project(Project(), None)

    def open_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Apri video della partita", self._last_dir(), VIDEO_FILTER)
        if path:
            self._use_video(path)

    def _use_video(self, path: str) -> None:
        if self.project.items and self.project.video_path and path != self.project.video_path:
            r = QMessageBox.question(self, "Cambia video",
                                     "Il progetto ha già delle clip. Usare questo video per le clip esistenti?\n"
                                     "(Scegli «No» per iniziare un nuovo progetto)")
            if r == QMessageBox.StandardButton.No:
                if not self._confirm_discard():
                    return
                self._set_project(Project(), None)
        self.load_video(path)
        self._mark_dirty()

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Apri progetto", self._last_dir(), PROJECT_FILTER)
        if path:
            self.load_project(path)

    def load_project(self, path: str) -> None:
        try:
            project = Project.load(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Errore", f"Impossibile aprire il progetto:\n{e}")
            return
        if project.video_path and not os.path.exists(project.video_path):
            QMessageBox.information(self, "Video non trovato",
                                    f"Il video «{os.path.basename(project.video_path)}» è stato spostato. "
                                    "Indica dove si trova ora.")
            new, _ = QFileDialog.getOpenFileName(self, "Trova il video", os.path.dirname(path), VIDEO_FILTER)
            project.video_path = new or ""
        self._remember_dir(path)
        self._set_project(project, path)
        if project.video_path:
            self.load_video(project.video_path)

    def save_project(self) -> bool:
        if not self.project_path:
            return self.save_project_as()
        try:
            self.project.save(self.project_path)
        except OSError as e:
            QMessageBox.critical(self, "Errore", f"Impossibile salvare:\n{e}")
            return False
        self.dirty = False
        self._refresh_title()
        self.statusBar().showMessage("Progetto salvato", 3000)
        return True

    def save_project_as(self) -> bool:
        default = os.path.splitext(self.project.video_path)[0] + PROJECT_EXT if self.project.video_path else ""
        path, _ = QFileDialog.getSaveFileName(self, "Salva progetto", default or self._last_dir(), PROJECT_FILTER)
        if not path:
            return False
        if not path.endswith(PROJECT_EXT):
            path += PROJECT_EXT
        self.project_path = path
        self._remember_dir(path)
        return self.save_project()

    def closeEvent(self, event) -> None:
        if self._confirm_discard():
            self._save_settings()
            self.player.stop()
            event.accept()
        else:
            event.ignore()

    # ------------------------------------------------- trascina e rilascia

    def dragEnterEvent(self, event) -> None:
        if any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        path = paths[0]
        if path.lower().endswith(PROJECT_EXT):
            if self._confirm_discard():
                self.load_project(path)
        elif path.lower().endswith(VIDEO_EXTS):
            self._use_video(path)
        else:
            QMessageBox.information(self, "File non supportato",
                                    "Trascina un video (mp4, mov, …) o un progetto .hvproj.")
        event.acceptProposedAction()

    # ============================================================ export

    def _default_export_dir(self) -> str:
        for p in (self.project_path, self.project.video_path):
            if p:
                return os.path.dirname(p)
        return self._last_dir()

    def export_selected(self) -> None:
        item = self.current_item()
        if not item:
            QMessageBox.information(self, "Esporta", "Seleziona una clip o copertina nella Sequenza.")
            return
        default = os.path.join(self._default_export_dir(), safe_filename(item_name(item)) + ".mp4")
        path, _ = QFileDialog.getSaveFileName(self, "Esporta elemento", default, "Video MP4 (*.mp4)")
        if path:
            self._start_export([item], path if path.lower().endswith(".mp4") else path + ".mp4", False)

    def export_all(self) -> None:
        if not self.project.items:
            QMessageBox.information(self, "Esporta", "Non ci sono clip da esportare.")
            return
        base = os.path.splitext(os.path.basename(self.project_path or self.project.video_path or "analisi"))[0]
        default = os.path.join(self._default_export_dir(), f"{base} - analisi.mp4")
        path, _ = QFileDialog.getSaveFileName(self, "Esporta tutto in un video", default, "Video MP4 (*.mp4)")
        if path:
            self._start_export(list(self.project.items), path if path.lower().endswith(".mp4") else path + ".mp4", False)

    def export_separate(self) -> None:
        if not self.project.items:
            QMessageBox.information(self, "Esporta", "Non ci sono clip da esportare.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Cartella di destinazione", self._default_export_dir())
        if folder:
            self._start_export(list(self.project.items), folder, True)

    def _start_export(self, items: list[Item], target: str, separate: bool) -> None:
        self.player.pause()
        snapshot = copy.deepcopy(self.project)
        ids = [i.id for i in items]
        worker = ExportWorker(snapshot, [snapshot.find(i) for i in ids], target, separate)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_export_progress)
        worker.done.connect(self._on_export_done)
        worker.failed.connect(self._on_export_failed)

        dlg = QProgressDialog("Esportazione in corso…", "Annulla", 0, 1000, self)
        dlg.setWindowTitle("Esporta")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumDuration(0)
        dlg.canceled.connect(self._on_export_cancel)
        dlg.show()
        self._export = (thread, worker, dlg, separate)
        thread.start()

    def _on_export_progress(self, f: float) -> None:
        if self._export:
            self._export[2].setValue(int(f * 1000))

    def _on_export_cancel(self) -> None:
        if self._export:
            self._export[1].cancelled = True

    def _finish_export(self) -> None:
        thread, worker, dlg, _ = self._export
        self._export = None
        dlg.close()
        thread.quit()
        thread.wait()
        worker.deleteLater()

    def _on_export_done(self, target: str) -> None:
        separate = self._export[3]
        self._finish_export()
        folder = target if separate else os.path.dirname(target)
        box = QMessageBox(self)
        box.setWindowTitle("Esportazione completata")
        box.setText("Video esportato con successo.")
        open_btn = box.addButton("Apri cartella", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() == open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _on_export_failed(self, message: str) -> None:
        self._finish_export()
        if message:
            QMessageBox.critical(self, "Errore di esportazione", message)
        else:
            self.statusBar().showMessage("Esportazione annullata", 4000)
