"""Genera la video guida dell'app, con narrazione e sottotitoli.

    .venv/bin/python tools/make_guide.py PERCORSO_VIDEO_PARTITA [-o guida.mp4]

L'app viene aperta "invisibile" (Qt offscreen) e pilotata da uno script:
ogni sezione ha un titolo, un testo letto dalla voce italiana di macOS
(comando `say`) e una serie di azioni (movimenti del cursore, clic,
digitazione). I fotogrammi vengono catturati in tempo reale e montati con
FFmpeg insieme alla narrazione.

I tempi e le posizioni dei disegni in SECTIONS si riferiscono alla partita
Ravarino – Carpine: con un altro video vanno adattati.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

W, H, FPS = 1920, 1080, 15
VOICE, RATE = "Alice", 178

TMP = tempfile.mkdtemp(prefix="hbv_guide_")
with open(os.path.join(TMP, "screen.json"), "w") as f:
    json.dump({"screens": [{"name": "guide", "x": 0, "y": 0, "width": W, "height": H,
                            "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}, f)
os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={os.path.join(TMP, 'screen.json')}"
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false;qt.qpa*=false")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QKeyEvent, QLinearGradient, QPainter,  # noqa: E402
                           QPainterPath, QPen)
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QMessageBox,  # noqa: E402
                               QPlainTextEdit, QPushButton, QWidget)

from handballvideo.app import STYLE, _dark_palette  # noqa: E402
from handballvideo.exporter import ffmpeg_exe  # noqa: E402
from handballvideo.main_window import MainWindow  # noqa: E402

FFMPEG = ffmpeg_exe()
EXPORT_PATH = os.path.join(TMP, "export_demo.mp4")


# ====================================================================== voce

def synth(text: str, path: str) -> float:
    subprocess.run(["say", "-v", VOICE, "-r", str(RATE), "-o", path, text], check=True)
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


_NARRATION: dict[str, tuple[str, float]] = {}


def prepare_narrations() -> None:
    """Sintetizza in anticipo tutti i testi passati a section(), in parallelo."""
    tree = ast.parse(open(__file__, encoding="utf-8").read())
    texts = [n.args[1].value for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "section"
             and len(n.args) > 1 and isinstance(n.args[1], ast.Constant)]
    paths = [os.path.join(TMP, f"n{i:02d}.aiff") for i in range(len(texts))]
    with ThreadPoolExecutor(max_workers=6) as pool:
        durations = list(pool.map(synth, texts, paths))
    _NARRATION.update({t: (p, d) for t, p, d in zip(texts, paths, durations)})


def narration(text: str) -> tuple[str, float]:
    if text not in _NARRATION:
        path = os.path.join(TMP, f"x{len(_NARRATION):02d}.aiff")
        _NARRATION[text] = (path, synth(text, path))
    return _NARRATION[text]


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?:])\s+", text.strip()) if s]


# ================================================================ registratore

class Recorder:
    """Cattura la finestra a FPS costanti (duplicando i fotogrammi se in ritardo)."""

    def __init__(self, window: QWidget, out: str) -> None:
        self.window = window
        self.cursor = QPointF(W * 0.5, H * 0.55)
        self.click_at: float | None = None
        self.chip = ""
        self.subtitle_text = ""
        self.subtitle_start = 0.0
        self.subtitle_parts: list[tuple[float, float, str]] = []
        self.card: tuple[str, str] | None = None
        self.card_off_at: float | None = None
        self.written = 0
        self.proc = subprocess.Popen(
            [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}",
             "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", out], stdin=subprocess.PIPE)
        self.t0 = time.monotonic()
        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(1000 / FPS))

    def now(self) -> float:
        return time.monotonic() - self.t0

    def set_subtitle(self, text: str, duration: float) -> None:
        parts = sentences(text)
        total = sum(len(p) for p in parts) or 1
        t, self.subtitle_parts = self.now(), []
        for p in parts:
            d = duration * len(p) / total
            self.subtitle_parts.append((t, t + d, p))
            t += d

    def tick(self) -> None:
        target = int(self.now() * FPS) + 1
        if target <= self.written:
            return
        img = self.compose()
        data = bytes(img.constBits())
        while self.written < target:
            self.proc.stdin.write(data)
            self.written += 1

    def stop(self) -> None:
        self.timer.stop()
        self.tick()
        self.proc.stdin.close()
        self.proc.wait()

    # ---- composizione del fotogramma -----------------------------------

    def compose(self) -> QImage:
        img = self.window.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        if img.width() != W or img.height() != H:
            img = img.scaled(W, H)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        origin = self.window.mapToGlobal(QPoint(0, 0))
        for tl in QApplication.topLevelWidgets():
            if tl is self.window or not tl.isVisible() or not isinstance(tl, QDialog):
                continue
            pos = tl.mapToGlobal(QPoint(0, 0)) - origin
            r = QRectF(pos.x(), pos.y(), tl.width(), tl.height())
            p.fillRect(QRectF(0, 0, W, H), QColor(0, 0, 0, 70))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawRoundedRect(r.adjusted(-2, 4, 6, 12), 10, 10)
            p.drawPixmap(pos, tl.grab())
        self._draw_chip(p)
        self._draw_subtitle(p)
        self._draw_cursor(p)
        self._draw_card(p)
        p.end()
        return img

    def _draw_chip(self, p: QPainter) -> None:
        if not self.chip:
            return
        font = QFont("Helvetica Neue")
        font.setPixelSize(22)
        font.setBold(True)
        p.setFont(font)
        tw = QFontMetricsF(font).horizontalAdvance(self.chip)
        r = QRectF(W - tw - 70, 10, tw + 44, 40)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#2d6cdf"))
        p.drawRoundedRect(r, 20, 20)
        p.setPen(QColor("#ffffff"))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), self.chip)

    def _draw_subtitle(self, p: QPainter) -> None:
        t = self.now()
        text = next((s for a, b, s in self.subtitle_parts if a <= t < b + 0.4), "")
        if not text:
            return
        font = QFont("Helvetica Neue")
        font.setPixelSize(30)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        flags = int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap)
        box = QRectF(W * 0.12, 0, W * 0.6, 200)
        br = QFontMetricsF(font).boundingRect(box, flags, text)
        r = QRectF(br.x() - 22, 0, br.width() + 44, br.height() + 22)
        r.moveCenter(QPointF(W * 0.42, 700 - r.height() / 2))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(10, 12, 16, 215))
        p.drawRoundedRect(r, 12, 12)
        p.setPen(QColor("#ffffff"))
        p.drawText(r, flags, text)

    def _draw_cursor(self, p: QPainter) -> None:
        if self.card and self.card_off_at is None:
            return
        x, y = self.cursor.x(), self.cursor.y()
        if self.click_at is not None:
            age = self.now() - self.click_at
            if age < 0.45:
                radius = 10 + age * 70
                c = QColor(255, 212, 0, int(220 * (1 - age / 0.45)))
                p.setPen(QPen(c, 4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(x, y), radius, radius)
        path = QPainterPath()
        pts = [(0, 0), (0, 25), (6.5, 19), (11, 29), (15, 27), (10.5, 17.5), (19, 17.5)]
        path.moveTo(x, y)
        for dx, dy in pts[1:]:
            path.lineTo(x + dx, y + dy)
        path.closeSubpath()
        p.setPen(QPen(QColor("#000000"), 1.6))
        p.setBrush(QColor("#ffffff"))
        p.drawPath(path)

    def _draw_card(self, p: QPainter) -> None:
        if not self.card:
            return
        alpha = 1.0
        if self.card_off_at is not None:
            alpha = max(0.0, 1 - (self.now() - self.card_off_at) / 0.7)
            if alpha <= 0:
                self.card, self.card_off_at = None, None
                return
        p.setOpacity(alpha)
        g = QLinearGradient(0, 0, W, H)
        g.setColorAt(0, QColor("#0B3D91"))
        g.setColorAt(1, QColor("#06173a"))
        p.fillRect(QRectF(0, 0, W, H), g)
        title, sub = self.card
        p.setPen(QColor("#ffffff"))
        f = QFont("Helvetica Neue")
        f.setBold(True)
        f.setPixelSize(96)
        p.setFont(f)
        p.drawText(QRectF(0, H * 0.30, W, 140), int(Qt.AlignmentFlag.AlignCenter), title)
        p.fillRect(QRectF(W / 2 - 70, H * 0.30 + 160, 140, 6), QColor("#FFD400"))
        f.setBold(False)
        f.setPixelSize(44)
        p.setFont(f)
        p.drawText(QRectF(0, H * 0.30 + 190, W, 120), int(Qt.AlignmentFlag.AlignCenter), sub)
        p.setOpacity(1.0)


# ==================================================================== regista

class Director:
    def __init__(self, app: QApplication, win: MainWindow, video: str, out: str) -> None:
        self.app, self.win, self.video, self.out = app, win, video, out
        self.narrations: list[tuple[float, str]] = []
        self.rec: Recorder | None = None
        self.gen = None

    # ---- motore --------------------------------------------------------

    def start(self) -> None:
        self.rec = Recorder(self.win, os.path.join(TMP, "screen.mp4"))
        self.gen = self.script()
        QTimer.singleShot(300, self._step)

    def _step(self) -> None:
        try:
            delay = next(self.gen)
        except StopIteration:
            self.finish()
            return
        except Exception:
            import traceback
            traceback.print_exc()
            self.finish()
            return
        QTimer.singleShot(max(1, int(delay * 1000)), self._step)

    def finish(self) -> None:
        self.rec.stop()
        self.win.dirty = False
        self.app.quit()

    # ---- primitive -----------------------------------------------------

    def pos_of(self, widget: QWidget, local: QPoint | None = None) -> QPointF:
        local = local if local is not None else widget.rect().center()
        g = widget.mapToGlobal(local) - self.win.mapToGlobal(QPoint(0, 0))
        return QPointF(g)

    def move(self, target: QPointF, dur: float = 0.55):
        start = QPointF(self.rec.cursor)
        steps = max(1, int(dur * 30))
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)
            self.rec.cursor = start + (target - start) * e
            yield 1 / 30

    def later(self, fn) -> None:
        # Le azioni che possono aprire finestre modali partono dopo il yield,
        # così il loop annidato della finestra non blocca il generatore.
        QTimer.singleShot(0, fn)

    def click(self, widget: QWidget, local: QPoint | None = None, dur: float = 0.55):
        local = local if local is not None else widget.rect().center()
        yield from self.move(self.pos_of(widget, local), dur)
        self.rec.click_at = self.rec.now()
        self.later(lambda: QTest.mouseClick(widget, Qt.MouseButton.LeftButton,
                                            Qt.KeyboardModifier.NoModifier, local))
        yield 0.35

    def type(self, widget: QWidget, text: str, enter: bool = False, per_char: float = 0.06):
        widget.setFocus()
        for ch in text:
            if ch.isascii():
                QTest.keyClicks(widget, ch)
            else:
                # QTest non gestisce i caratteri non ASCII (è, –): evento con solo testo.
                for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                    QApplication.sendEvent(widget, QKeyEvent(kind, Qt.Key.Key_unknown,
                                                             Qt.KeyboardModifier.NoModifier, ch))
            yield per_char
        if enter:
            QTest.keyClick(widget, Qt.Key.Key_Return)
            yield 0.2

    def select_all(self, widget: QWidget):
        widget.setFocus()
        QTest.keyClick(widget, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        if hasattr(widget, "selectAll"):
            widget.selectAll()
        yield 0.1

    def drag(self, widget: QWidget, a: QPoint, b: QPoint, dur: float = 0.8):
        yield from self.move(self.pos_of(widget, a))
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
        self.rec.click_at = self.rec.now()
        steps = max(2, int(dur * 30))
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)
            pt = QPoint(int(a.x() + (b.x() - a.x()) * e), int(a.y() + (b.y() - a.y()) * e))
            QTest.mouseMove(widget, pt)
            self.rec.cursor = self.pos_of(widget, pt)
            yield 1 / 30
        self.later(lambda: QTest.mouseRelease(widget, Qt.MouseButton.LeftButton,
                                              Qt.KeyboardModifier.NoModifier, b))
        yield 0.35

    def video_point(self, x: float, y: float) -> QPoint:
        v = self.win.view
        return v.mapFromScene(QPointF(x * v.overlay.w, y * v.overlay.h))

    def wait_until(self, cond, timeout: float = 20.0):
        t = time.monotonic()
        while not cond() and time.monotonic() - t < timeout:
            yield 0.1

    def modal(self, kind):
        w = QApplication.activeModalWidget()
        return w if isinstance(w, kind) else None

    def section(self, title: str, text: str, body=None, tail: float = 0.8):
        self.rec.chip = title
        path, duration = narration(text)
        self.narrations.append((self.rec.now(), path))
        self.rec.set_subtitle(text, duration)
        start = self.rec.now()
        if body:
            yield from body()
        remaining = duration + tail - (self.rec.now() - start)
        if remaining > 0:
            yield remaining

    def button(self, text: str = "", tooltip: str = "") -> QPushButton:
        for b in self.win.findChildren(QPushButton):
            if (text and b.text() == text) or (tooltip and b.toolTip() == tooltip):
                return b
        raise LookupError(text or tooltip)

    def seq_item(self, row: int):
        seq = self.win.seq
        return seq.viewport(), seq.visualItemRect(seq.item(row)).center()

    # ---- copione -------------------------------------------------------

    def script(self):
        w = self.win

        # Intro
        self.rec.card = ("Handball Video", "Guida rapida")
        yield from self.section("", "Benvenuto in Handball Video. In pochi minuti vediamo come trasformare "
                                    "il video di una partita in un montaggio di analisi, con clip, disegni e copertine.",
                                tail=0.4)
        self.rec.card_off_at = self.rec.now()
        yield 0.6

        def toolbar_widget(action):
            return next(o for o in action.associatedObjects() if isinstance(o, QWidget) and o is not w.menuBar()
                        and o.metaObject().className() == "QToolButton")

        def open_video():
            yield from self.click(toolbar_widget(w.act_open_video))
            yield 1.5
        yield from self.section("1 · Apri il video",
                                "Per iniziare, clicca Apri video e scegli il file della partita. "
                                "Puoi anche trascinarlo direttamente dentro la finestra.", open_video)

        def navigate():
            yield from self.click(w.time_field)
            yield from self.type(w.time_field, "14:38", enter=True)
            yield 0.8
            yield from self.click(w.btn_play)
            yield 2.5
            yield from self.move(self.pos_of(self.button(tooltip="Indietro 5 secondi (J)")))
            yield 0.4
            yield from self.move(self.pos_of(self.button(tooltip="Fotogramma successivo (→)")))
            yield 0.5
            yield from self.click(w.btn_play)
        yield from self.section("2 · Muoviti nel video",
                                "Con play, o con la barra spaziatrice, avvii la riproduzione. "
                                "Puoi scrivere un tempo nel contatore per saltare a un punto preciso, "
                                "e usare questi pulsanti per spostarti di cinque secondi o di un fotogramma.",
                                navigate)

        def create_clip():
            w.seek(877)
            yield 0.5
            yield from self.click(w.btn_play)
            yield from self.wait_until(lambda: w.position() >= 880)
            yield from self.click(self.button("Inizio (I)"), dur=0.4)
            yield from self.move(self.pos_of(self.button("Fine (O)")), 0.8)
            yield from self.wait_until(lambda: w.position() >= 891)
            yield from self.click(self.button("Fine (O)"), dur=0.2)
            yield from self.click(w.btn_play)
            yield from self.click(w.clip_name)
            yield from self.type(w.clip_name, "Attacco posizionale Carpine")
            yield from self.click(self.button("Aggiungi clip"))
        yield from self.section("3 · Crea una clip",
                                "Per creare una clip, porta il video all'inizio dell'azione e premi Inizio, "
                                "oppure il tasto I. Quando l'azione finisce premi Fine, oppure il tasto O. "
                                "Poi scrivi un nome e clicca Aggiungi clip.", create_clip)

        def typed_times():
            yield from self.click(w.in_field)
            yield from self.type(w.in_field, "14:52", enter=True)
            yield from self.click(w.out_field)
            yield from self.type(w.out_field, "15:01", enter=True)
            yield from self.click(w.clip_name)
            yield from self.type(w.clip_name, "Gol Ravarino")
            yield from self.click(self.button("Aggiungi clip"))
        yield from self.section("4 · Scrivi i tempi",
                                "Se conosci già i tempi, puoi scriverli direttamente nei campi "
                                "accanto a Inizio e Fine.", typed_times)

        def quick_tags():
            w.seek(915)
            yield from self.click(w.btn_play)
            yield 2.5
            yield from self.click(self.button("Attacco"))
            yield 4.5
            yield from self.click(self.button("Parata"))
            yield 1.0
            yield from self.click(w.btn_play)
        yield from self.section("5 · Azioni rapide",
                                "Ancora più veloce: mentre guardi la partita, clicca un'azione rapida, "
                                "come Attacco, Gol o Parata, oppure premi i tasti da uno a nove. "
                                "La clip viene creata in automatico, da otto secondi prima a due secondi dopo, "
                                "ed è colorata in base al tipo.", quick_tags)

        def timeline():
            tl = w.timeline
            yield from self.move(self.pos_of(tl, QPoint(tl.width() // 2, tl.height() // 2)))
            target_start, target_len = 860.0, 100.0
            for i in range(1, 21):
                f = i / 20
                vl = tl._vl() + (target_len - tl._vl()) * f
                tl._set_view(tl.view_start + (target_start - tl.view_start) * f, vl)
                yield 1 / 20
            yield 0.8
            clip = next(c for c in w.project.items if getattr(c, "name", "") == "Attacco posizionale Carpine")
            y = int(tl._track().center().y())
            a = QPoint(int(tl._x(clip.end)), y)
            b = QPoint(int(tl._x(clip.end + 2.5)), y)
            yield from self.drag(tl, a, b, dur=1.0)
        yield from self.section("6 · La timeline",
                                "In basso trovi la timeline, con tutte le clip della partita. "
                                "Con la rotellina del mouse fai zoom, e trascinando il bordo di una clip "
                                "la allunghi o la accorci.", timeline)

        v = w.view.viewport()

        def draw():
            yield from self.click(*self.seq_item(0))
            yield 0.6
            w.seek(884)
            yield 0.8
            yield from self.click(w.tool_group.button(1))
            yield from self.drag(v, self.video_point(0.42, 0.62), self.video_point(0.53, 0.70))
            yield from self.click(w.tool_group.button(2))
            yield from self.drag(v, self.video_point(0.44, 0.47), self.video_point(0.64, 0.45))
            yield from self.click(w.tool_group.button(5))
            yield from self.drag(v, self.video_point(0.635, 0.40), self.video_point(0.695, 0.63))
        yield from self.section("7 · Disegna sulla clip",
                                "Ora aggiungiamo i disegni. Seleziona la clip nella sequenza a destra, "
                                "porta il video nel punto giusto e scegli uno strumento: la freccia per il "
                                "movimento di un giocatore, la freccia tratteggiata per un passaggio, "
                                "e il cerchio per evidenziare un giocatore.", draw)

        def text_box():
            yield from self.click(w.tool_group.button(6))
            yield from self.drag(v, self.video_point(0.33, 0.06), self.video_point(0.62, 0.19))
            yield from self.wait_until(lambda: self.modal(QDialog) is not None, 5)
            dlg = self.modal(QDialog)
            edit = dlg.findChild(QPlainTextEdit) if dlg else None
            if edit:
                yield from self.move(self.pos_of(edit))
                yield from self.type(edit, "Il pivot n. 6 è libero: cercalo!", per_char=0.05)
                ok = dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
                yield from self.click(ok)
        yield from self.section("8 · Aggiungi un commento",
                                "Con lo strumento Testo disegni un riquadro e scrivi il tuo commento.", text_box)

        def edit_drawings():
            yield from self.click(w.tool_group.button(0))
            yield from self.drag(v, self.video_point(0.665, 0.40), self.video_point(0.672, 0.40), dur=0.5)
            yield from self.click(v, self.video_point(0.47, 0.12))
            yield from self.drag(v, self.video_point(0.62, 0.19), self.video_point(0.64, 0.21), dur=0.6)
            yield 0.5
            ann = w.ann_list
            yield from self.click(ann.viewport(), ann.visualItemRect(ann.item(3)).center())
            yield from self.click(w.a_to_end)
        yield from self.section("9 · Modifica i disegni",
                                "Con il puntatore puoi selezionare un disegno, spostarlo, o ridimensionarlo "
                                "dai quadratini. Nel pannello a destra scegli quando appare e quando sparisce.",
                                edit_drawings)

        def replay():
            yield from self.click(self.button("Riproduci clip"))
            yield 7.0
        yield from self.section("10 · Rivedi la clip",
                                "Premi Riproduci clip per vedere il risultato: i disegni compaiono "
                                "e scompaiono nei tempi che hai scelto.", replay)

        def cover():
            yield from self.click(*self.seq_item(0))
            yield from self.click(self.button("Copertina"))
            yield 0.4
            yield from self.select_all(w.c_title)
            yield from self.type(w.c_title, "Ravarino – Carpine")
            yield from self.click(w.c_sub)
            yield from self.type(w.c_sub, "Analisi primo tempo")
            yield from self.move(self.pos_of(w.c_bg))
            yield 0.4
            yield from self.move(self.pos_of(w.c_dur))
        yield from self.section("11 · La copertina",
                                "Per aggiungere una copertina, seleziona il primo elemento e clicca Copertina. "
                                "Scrivi titolo e sottotitolo, e scegli colori e durata.", cover)

        def reorder():
            last = w.seq.count() - 1
            yield from self.click(*self.seq_item(last))
            up = self.button(tooltip="Sposta su")
            yield from self.click(up)
            yield from self.click(up)
        yield from self.section("12 · Ordina la sequenza",
                                "Puoi cambiare l'ordine degli elementi trascinandoli, "
                                "oppure con le frecce su e giù.", reorder)

        def export():
            yield from self.click(toolbar_widget(w.act_export_all))
            yield from self.wait_until(lambda: self.modal(QMessageBox) is not None, 240)
            yield 1.2
            box = self.modal(QMessageBox)
            if box:
                ok = next(b for b in box.buttons() if box.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole)
                yield from self.click(ok)
        yield from self.section("13 · Esporta",
                                "Quando hai finito, clicca Esporta tutto in un video: clip, disegni e copertine "
                                "vengono uniti in un unico file, pronto da condividere. Dal menu Esporta puoi "
                                "anche salvare una sola clip, oppure un file per ogni clip.", export)

        self.rec.chip = ""
        self.rec.card, self.rec.card_off_at = ("Buona analisi!", "Ricordati di salvare il progetto"), None
        yield from self.section("", "Ricordati di salvare il progetto, così potrai riprendere il lavoro "
                                    "quando vuoi. Buona analisi!", tail=1.5)

    # ---- montaggio finale ---------------------------------------------

    def mux(self) -> None:
        screen = os.path.join(TMP, "screen.mp4")
        args = [FFMPEG, "-y", "-loglevel", "error", "-i", screen]
        graph = []
        for i, (t, path) in enumerate(self.narrations, 1):
            args += ["-i", path]
            ms = int(t * 1000)
            graph.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms}[a{i}]")
        graph.append("".join(f"[a{i}]" for i in range(1, len(self.narrations) + 1))
                     + f"amix=inputs={len(self.narrations)}:normalize=0,apad[a]")
        duration = self.rec.written / FPS
        args += ["-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[a]", "-t", f"{duration:.3f}",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", self.out]
        subprocess.run(args, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video")
    parser.add_argument("-o", "--output", default=os.path.join(ROOT, "guida-handball-video.mp4"))
    args = parser.parse_args()
    video = os.path.abspath(args.video)
    print("Preparo la narrazione…", flush=True)
    prepare_narrations()

    # Le finestre di sistema per scegliere i file non si possono registrare:
    # rispondiamo noi al posto dell'utente.
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (video, ""))
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (EXPORT_PATH, ""))

    app = QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.setGeometry(0, 0, W, H)
    win.act_timeline.setChecked(True)
    win.timeline_box.setVisible(True)
    win.splitter.setSizes([W - 430, 430])
    win.show()

    director = Director(app, win, video, os.path.abspath(args.output))
    director.start()
    app.exec()
    print("Montaggio audio…", flush=True)
    director.mux()
    print(f"Guida creata: {director.out}")


if __name__ == "__main__":
    main()
