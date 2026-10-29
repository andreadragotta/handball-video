"""Timeline della partita: righello, clip, segni di inizio/fine e cursore.

- clic / trascinamento su zona vuota: sposta il video
- clic su una clip: la seleziona
- trascinamento dei bordi di una clip: la accorcia o allunga
- rotellina: zoom attorno al mouse; Shift + rotellina (o scroll orizzontale): scorre
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from .model import Clip, fmt_time

RULER_H = 22
EDGE_PX = 6
MIN_CLIP = 0.2
MIN_VIEW = 4.0
DEFAULT_CLIP_COLOR = "#3d7be0"
TICK_STEPS = (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)


class Timeline(QWidget):
    seek_requested = Signal(float)
    clip_clicked = Signal(str)
    clip_trimmed = Signal(str)        # durante il trascinamento di un bordo
    trim_finished = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(92)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.position = 0.0
        self.clips: list[Clip] = []
        self.tag_colors: dict[str, str] = {}
        self.selected_id: Optional[str] = None
        self.mark_in: Optional[float] = None
        self.mark_out: Optional[float] = None
        self.view_start = 0.0
        self.view_len = 0.0  # 0 = tutta la partita
        self._drag: Optional[tuple] = None

    # ---- stato --------------------------------------------------------

    def set_duration(self, seconds: float) -> None:
        self.duration = max(0.0, seconds)
        self.view_start, self.view_len = 0.0, 0.0
        self.update()

    def set_position(self, seconds: float, follow: bool = False) -> None:
        self.position = seconds
        if follow and self.view_len:
            vs, vl = self.view_start, self._vl()
            if not vs <= seconds <= vs + vl:
                self._set_view(seconds - vl * 0.1, vl)
        self.update()

    def fit(self) -> None:
        self.view_start, self.view_len = 0.0, 0.0
        self.update()

    def zoom(self, factor: float, anchor: Optional[float] = None) -> None:
        if self.duration <= 0:
            return
        vl = self._vl()
        anchor = self.position if anchor is None else anchor
        new_len = min(max(vl * factor, MIN_VIEW), self.duration)
        start = anchor - (anchor - self.view_start) * new_len / vl
        self._set_view(start, new_len)

    def _set_view(self, start: float, length: float) -> None:
        if length >= self.duration:
            self.view_start, self.view_len = 0.0, 0.0
        else:
            self.view_start = min(max(start, 0.0), self.duration - length)
            self.view_len = length
        self.update()

    # ---- geometria ----------------------------------------------------

    def _vl(self) -> float:
        return self.view_len or max(self.duration, 1.0)

    def _x(self, t: float) -> float:
        return (t - self.view_start) / self._vl() * self.width()

    def _t(self, x: float) -> float:
        t = self.view_start + x / max(self.width(), 1) * self._vl()
        return min(max(t, 0.0), self.duration)

    def _track(self) -> QRectF:
        return QRectF(0, RULER_H + 6, self.width(), self.height() - RULER_H - 14)

    def _clip_rect(self, c: Clip) -> QRectF:
        tr = self._track()
        x1, x2 = self._x(c.start), self._x(c.end)
        return QRectF(x1, tr.top(), max(x2 - x1, 2.0), tr.height())

    def _edge_at(self, x: float, y: float) -> Optional[tuple[Clip, str]]:
        if not self._track().contains(QPointF(self.width() / 2, y)):
            return None
        # La clip selezionata ha la precedenza quando i bordi si sovrappongono.
        ordered = sorted(self.clips, key=lambda c: c.id != self.selected_id)
        for c in ordered:
            if abs(self._x(c.start) - x) <= EDGE_PX:
                return c, "start"
            if abs(self._x(c.end) - x) <= EDGE_PX:
                return c, "end"
        return None

    def _clip_at(self, x: float, y: float) -> Optional[Clip]:
        hits = [c for c in self.clips if self._clip_rect(c).contains(QPointF(x, y))]
        if not hits:
            return None
        return next((c for c in hits if c.id == self.selected_id), hits[-1])

    # ---- disegno ------------------------------------------------------

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#1b1c1f"))
        p.fillRect(QRectF(0, 0, w, RULER_H), QColor("#26272b"))
        font = QFont(self.font())
        font.setPixelSize(11)
        p.setFont(font)

        if self.duration <= 0:
            p.setPen(QColor("#777"))
            p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), "Apri un video per vedere la timeline")
            return

        # righello
        pps = w / self._vl()
        step = next((s for s in TICK_STEPS if s * pps >= 80), TICK_STEPS[-1])
        t = (int(self.view_start / step)) * step
        end = self.view_start + self._vl()
        while t <= end + step:
            x = self._x(t)
            p.setPen(QColor("#5a5c63"))
            p.drawLine(QPointF(x, RULER_H - 7), QPointF(x, RULER_H))
            xm = self._x(t + step / 2)
            p.drawLine(QPointF(xm, RULER_H - 3), QPointF(xm, RULER_H))
            p.setPen(QColor("#a8a8a8"))
            p.drawText(QPointF(x + 3, 12), fmt_time(t, precise=step < 1))
            t += step

        # zona inizio/fine in preparazione
        tr = self._track()
        green = QColor("#34c759")
        if self.mark_in is not None:
            x1 = self._x(self.mark_in)
            if self.mark_out is not None:
                fill = QColor(green)
                fill.setAlpha(45)
                p.fillRect(QRectF(x1, RULER_H, self._x(self.mark_out) - x1, h - RULER_H), fill)
            p.setPen(QPen(green, 2))
            p.drawLine(QPointF(x1, RULER_H), QPointF(x1, h))
        if self.mark_out is not None:
            p.setPen(QPen(green, 2))
            x2 = self._x(self.mark_out)
            p.drawLine(QPointF(x2, RULER_H), QPointF(x2, h))

        # clip
        fm = QFontMetrics(font)
        for c in self.clips:
            r = self._clip_rect(c)
            if r.right() < 0 or r.left() > w:
                continue
            color = QColor(self.tag_colors.get(c.tag, DEFAULT_CLIP_COLOR))
            selected = c.id == self.selected_id
            if not selected:
                color.setAlpha(190)
            p.setPen(QPen(QColor("#ffffff"), 2) if selected else Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(r, 4, 4)
            if r.width() > 28:
                p.setPen(QColor("#ffffff"))
                text = fm.elidedText(c.name, Qt.TextElideMode.ElideRight, int(r.width() - 10))
                p.drawText(r.adjusted(6, 0, -4, 0),
                           int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
            if selected:
                # Tacche dei disegni della clip selezionata
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#FFD400"))
                for a in c.annotations:
                    ax = self._x(c.start + a.start)
                    p.drawPolygon(QPolygonF([QPointF(ax, r.bottom() - 7), QPointF(ax - 4, r.bottom() + 1),
                                             QPointF(ax + 4, r.bottom() + 1)]))
        if not self.clips:
            p.setPen(QColor("#666"))
            p.drawText(tr, int(Qt.AlignmentFlag.AlignCenter),
                       "Nessuna clip: usa Inizio/Fine o i pulsanti azione rapida")

        # cursore di riproduzione
        x = self._x(self.position)
        red = QColor("#ff453a")
        p.setPen(QPen(red, 2))
        p.drawLine(QPointF(x, 0), QPointF(x, h))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(red)
        p.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 8)]))

        # barra di scorrimento quando si è in zoom
        if self.view_len:
            bar_w = max(w * self.view_len / self.duration, 20)
            bar_x = (w - bar_w) * self.view_start / max(self.duration - self.view_len, 1e-6)
            p.setBrush(QColor(255, 255, 255, 60))
            p.drawRoundedRect(QRectF(bar_x, h - 5, bar_w, 4), 2, 2)

    # ---- mouse --------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.duration <= 0:
            return
        x, y = event.position().x(), event.position().y()
        edge = self._edge_at(x, y)
        if edge:
            self._drag = ("trim", edge[0], edge[1])
            if edge[0].id != self.selected_id:
                self.clip_clicked.emit(edge[0].id)
            return
        clip = self._clip_at(x, y)
        if clip:
            self.clip_clicked.emit(clip.id)
            self._drag = ("seek",)
            self.seek_requested.emit(self._t(x))
            return
        self._drag = ("seek",)
        self.seek_requested.emit(self._t(x))

    def mouseMoveEvent(self, event) -> None:
        x, y = event.position().x(), event.position().y()
        if self._drag is None:
            if self._edge_at(x, y):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif self._clip_at(x, y):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                c = self._clip_at(x, y)
                self.setToolTip(f"{c.name}\n{fmt_time(c.start, True)} – {fmt_time(c.end, True)}")
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self.setToolTip("")
            return
        t = self._t(x)
        if self._drag[0] == "seek":
            self.seek_requested.emit(t)
        else:
            _, clip, edge = self._drag
            if edge == "start":
                clip.start = min(t, clip.end - MIN_CLIP)
                self.seek_requested.emit(clip.start)
            else:
                clip.end = max(t, clip.start + MIN_CLIP)
                self.seek_requested.emit(clip.end)
            self.clip_trimmed.emit(clip.id)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag and self._drag[0] == "trim":
            self.trim_finished.emit(self._drag[1].id)
        self._drag = None

    def wheelEvent(self, event) -> None:
        if self.duration <= 0:
            return
        dx, dy = event.angleDelta().x(), event.angleDelta().y()
        if abs(dx) > abs(dy) or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            delta = dx or dy
            self._set_view(self.view_start - delta / 120 * self._vl() * 0.1, self._vl())
        elif dy:
            self.zoom(0.8 ** (dy / 120), self._t(event.position().x()))
        event.accept()
