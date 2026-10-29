"""Area video: player + livello di disegno delle annotazioni.

Con uno strumento attivo si disegna; con il puntatore si selezionano i
disegni visibili, si spostano trascinandoli e si ridimensionano dalle
maniglie (i due punti che li definiscono).
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import QFrame, QGraphicsItem, QGraphicsScene, QGraphicsView

from .model import Annotation, Cover
from .render import draw_annotation, draw_cover


class OverlayItem(QGraphicsItem):
    """Disegna sopra al video le annotazioni visibili (o la copertina)."""

    def __init__(self) -> None:
        super().__init__()
        self.w, self.h = 1920.0, 1080.0
        self.annotations: list[Annotation] = []
        self.preview: Optional[Annotation] = None
        self.cover: Optional[Cover] = None
        self.selected_id: Optional[str] = None
        self.placeholder: Optional[str] = None
        self.handle = 8.0  # lato maniglie in coordinate scena

    def set_size(self, w: float, h: float) -> None:
        self.prepareGeometryChange()
        self.w, self.h = w, h

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.w, self.h)

    def paint(self, p: QPainter, option, widget=None) -> None:
        if self.cover is not None:
            draw_cover(p, self.cover, self.w, self.h)
            return
        if self.placeholder:
            p.fillRect(self.boundingRect(), QColor("#141518"))
            font = QFont()
            font.setPixelSize(int(self.h * 0.035))
            p.setFont(font)
            p.setPen(QColor("#8a8c93"))
            p.drawText(self.boundingRect(), int(Qt.AlignmentFlag.AlignCenter), self.placeholder)
            return
        for a in self.annotations:
            draw_annotation(p, a, self.w, self.h)
            if a.id == self.selected_id:
                self._draw_selection(p, a)
        if self.preview is not None:
            draw_annotation(p, self.preview, self.w, self.h)

    def _draw_selection(self, p: QPainter, a: Annotation) -> None:
        p1, p2 = QPointF(a.x1 * self.w, a.y1 * self.h), QPointF(a.x2 * self.w, a.y2 * self.h)
        pen = QPen(QColor("#ffffff"), self.handle / 5, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if a.kind in ("rect", "ellipse", "text"):
            p.drawRect(QRectF(p1, p2).normalized())
        s = self.handle
        p.setPen(QPen(QColor("#2d6cdf"), s / 5))
        p.setBrush(QColor("#ffffff"))
        for pt in (p1, p2):
            p.drawRect(QRectF(pt.x() - s / 2, pt.y() - s / 2, s, s))


def _segment_distance(p: QPointF, a: QPointF, b: QPointF) -> float:
    dx, dy = b.x() - a.x(), b.y() - a.y()
    if dx == dy == 0:
        return math.hypot(p.x() - a.x(), p.y() - a.y())
    t = max(0.0, min(1.0, ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p.x() - (a.x() + t * dx), p.y() - (a.y() + t * dy))


class VideoView(QGraphicsView):
    annotation_drawn = Signal(object)        # Annotation nuova
    annotation_selected = Signal(str)        # id, "" = nessuna
    annotation_changed = Signal()            # durante lo spostamento
    annotation_edit_finished = Signal()
    annotation_double_clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.setBackgroundBrush(QColor("#000000"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumSize(480, 270)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self.video_item = QGraphicsVideoItem()
        self.overlay = OverlayItem()
        self.scene_.addItem(self.video_item)
        self.scene_.addItem(self.overlay)
        self.video_item.nativeSizeChanged.connect(self._on_native_size)

        self.tool: Optional[str] = None
        self.color = "#FFD400"
        self.width_rel = 0.007
        self._start: Optional[QPointF] = None
        self._edit: Optional[tuple] = None  # (annotation, modalità, punto iniziale, coordinate originali)
        self.set_frame_size(1920, 1080)

    # ---- dimensioni ---------------------------------------------------

    def _on_native_size(self, size: QSizeF) -> None:
        if not size.isEmpty():
            self.set_frame_size(size.width(), size.height())

    def set_frame_size(self, w: float, h: float) -> None:
        self.video_item.setSize(QSizeF(w, h))
        self.overlay.set_size(w, h)
        self.scene_.setSceneRect(0, 0, w, h)
        self._fit()

    def _fit(self) -> None:
        self.fitInView(self.scene_.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        scale = self.transform().m11() or 1.0
        self.overlay.handle = 10.0 / scale

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    # ---- disegno ------------------------------------------------------

    def set_tool(self, tool: Optional[str]) -> None:
        self.tool = tool
        self.viewport().setCursor(Qt.CursorShape.CrossCursor if tool else Qt.CursorShape.ArrowCursor)

    def _scene_point(self, event) -> QPointF:
        pt = self.mapToScene(event.position().toPoint())
        return QPointF(min(max(pt.x(), 0.0), self.overlay.w), min(max(pt.y(), 0.0), self.overlay.h))

    def _make(self, a: QPointF, b: QPointF) -> Annotation:
        w, h = self.overlay.w, self.overlay.h
        return Annotation(kind=self.tool or "arrow", x1=a.x() / w, y1=a.y() / h,
                          x2=b.x() / w, y2=b.y() / h, color=self.color, width=self.width_rel)

    # ---- selezione e modifica (strumento puntatore) -------------------

    def _tolerance(self) -> float:
        return 6.0 / (self.transform().m11() or 1.0)

    def _points(self, a: Annotation) -> tuple[QPointF, QPointF]:
        w, h = self.overlay.w, self.overlay.h
        return QPointF(a.x1 * w, a.y1 * h), QPointF(a.x2 * w, a.y2 * h)

    def _hit(self, a: Annotation, pt: QPointF) -> bool:
        p1, p2 = self._points(a)
        tol = self._tolerance() + a.width * self.overlay.h
        if a.kind in ("arrow", "pass", "line"):
            return _segment_distance(pt, p1, p2) <= tol
        return QRectF(p1, p2).normalized().adjusted(-tol, -tol, tol, tol).contains(pt)

    def _handle_at(self, pt: QPointF) -> Optional[tuple[Annotation, str]]:
        sel = next((a for a in self.overlay.annotations if a.id == self.overlay.selected_id), None)
        if sel is None:
            return None
        r = self.overlay.handle * 0.8 + self._tolerance()
        for name, hp in zip(("p1", "p2"), self._points(sel)):
            if abs(pt.x() - hp.x()) <= r and abs(pt.y() - hp.y()) <= r:
                return sel, name
        return None

    def _annotation_at(self, pt: QPointF) -> Optional[Annotation]:
        anns = self.overlay.annotations
        sel = next((a for a in anns if a.id == self.overlay.selected_id and self._hit(a, pt)), None)
        return sel or next((a for a in reversed(anns) if self._hit(a, pt)), None)

    def select_annotation(self, ann_id: Optional[str]) -> None:
        self.overlay.selected_id = ann_id
        self.overlay.update()

    def _pointer_press(self, event) -> None:
        pt = self._scene_point(event)
        handle = self._handle_at(pt)
        if handle:
            a, mode = handle
        else:
            a = self._annotation_at(pt)
            mode = "move"
            if a is None:
                if self.overlay.selected_id:
                    self.select_annotation(None)
                    self.annotation_selected.emit("")
                return
        if a.id != self.overlay.selected_id:
            self.select_annotation(a.id)
            self.annotation_selected.emit(a.id)
        self._edit = (a, mode, pt, (a.x1, a.y1, a.x2, a.y2), False)

    def _pointer_move(self, event) -> None:
        pt = self._scene_point(event)
        if self._edit is None:
            if self._handle_at(pt):
                cursor = Qt.CursorShape.SizeAllCursor
            elif self._annotation_at(pt):
                cursor = Qt.CursorShape.OpenHandCursor
            else:
                cursor = Qt.CursorShape.ArrowCursor
            self.viewport().setCursor(cursor)
            return
        a, mode, start, (x1, y1, x2, y2), _ = self._edit
        dx = (pt.x() - start.x()) / self.overlay.w
        dy = (pt.y() - start.y()) / self.overlay.h
        if mode == "move":
            # Lo spostamento non fa uscire il disegno dal fotogramma.
            dx = min(max(dx, -min(x1, x2)), 1 - max(x1, x2))
            dy = min(max(dy, -min(y1, y2)), 1 - max(y1, y2))
            a.x1, a.y1, a.x2, a.y2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy
        elif mode == "p1":
            a.x1, a.y1 = pt.x() / self.overlay.w, pt.y() / self.overlay.h
        else:
            a.x2, a.y2 = pt.x() / self.overlay.w, pt.y() / self.overlay.h
        self._edit = (*self._edit[:4], True)
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor if mode == "move" else Qt.CursorShape.SizeAllCursor)
        self.overlay.update()
        self.annotation_changed.emit()

    def mouseDoubleClickEvent(self, event) -> None:
        if self.tool is None and not self.overlay.cover:
            a = self._annotation_at(self._scene_point(event))
            if a is not None:
                self.annotation_double_clicked.emit(a.id)
                return
        super().mouseDoubleClickEvent(event)

    # ---- disegno ------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.overlay.cover or self.overlay.placeholder:
            return super().mousePressEvent(event)
        if not self.tool:
            return self._pointer_press(event)
        self._start = self._scene_point(event)
        self.overlay.preview = self._make(self._start, self._start)
        self.overlay.update()

    def mouseMoveEvent(self, event) -> None:
        if not self.tool:
            return self._pointer_move(event)
        if self._start is None:
            return super().mouseMoveEvent(event)
        self.overlay.preview = self._make(self._start, self._scene_point(event))
        self.overlay.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._edit is not None:
            changed = self._edit[4]
            self._edit = None
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            if changed:
                self.annotation_edit_finished.emit()
            return
        if self._start is None:
            return super().mouseReleaseEvent(event)
        end = self._scene_point(event)
        start, self._start = self._start, None
        self.overlay.preview = None
        self.overlay.update()
        w, h = self.overlay.w, self.overlay.h
        tiny = abs(end.x() - start.x()) < w * 0.01 and abs(end.y() - start.y()) < h * 0.01
        if tiny:
            if self.tool != "text":
                return
            # Click semplice con lo strumento testo: riquadro di dimensione standard.
            end = QPointF(min(start.x() + w * 0.3, w), min(start.y() + h * 0.1, h))
        self.annotation_drawn.emit(self._make(start, end))
