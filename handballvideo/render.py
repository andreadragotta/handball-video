"""Disegno di annotazioni e copertine.

Le stesse funzioni servono sia per l'anteprima nell'editor sia per
generare le immagini PNG sovrapposte al video in fase di export,
così quello che vedi è quello che esporti.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen, QPolygonF

from .model import Annotation, Cover

FONT_FAMILY = "Arial"


def _contrast(color: QColor) -> QColor:
    lum = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return QColor("#000000") if lum > 150 else QColor("#FFFFFF")


def draw_annotation(p: QPainter, a: Annotation, w: float, h: float) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(a.color)
    lw = max(1.5, a.width * h)
    p1 = QPointF(a.x1 * w, a.y1 * h)
    p2 = QPointF(a.x2 * w, a.y2 * h)

    if a.kind == "text":
        _text_box(p, a, p1, p2, w, h)
    else:
        style = Qt.PenStyle.DashLine if a.kind == "pass" else Qt.PenStyle.SolidLine
        # Ombra leggermente spostata, poi il tratto colorato: il disegno
        # resta leggibile anche sul parquet chiaro.
        off = lw * 0.35
        for c, dx in ((QColor(0, 0, 0, 130), off), (color, 0.0)):
            p.save()
            p.translate(dx, dx)
            pen = QPen(c, lw, style, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            if a.kind in ("arrow", "pass"):
                _arrow(p, p1, p2, pen, c, lw)
            elif a.kind == "line":
                p.setPen(pen)
                p.drawLine(p1, p2)
            elif a.kind in ("rect", "ellipse"):
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                r = QRectF(p1, p2).normalized()
                p.drawRect(r) if a.kind == "rect" else p.drawEllipse(r)
            p.restore()
    p.restore()


def _arrow(p: QPainter, a: QPointF, b: QPointF, pen: QPen, color: QColor, lw: float) -> None:
    dx, dy = b.x() - a.x(), b.y() - a.y()
    length = math.hypot(dx, dy)
    if length < 1:
        return
    ux, uy = dx / length, dy / length
    head = min(max(lw * 4.0, 14.0), length)
    base = QPointF(b.x() - ux * head * 0.9, b.y() - uy * head * 0.9)
    p.setPen(pen)
    p.drawLine(a, base)
    px, py = -uy * head * 0.55, ux * head * 0.55
    tri = QPolygonF([
        b,
        QPointF(b.x() - ux * head + px, b.y() - uy * head + py),
        QPointF(b.x() - ux * head - px, b.y() - uy * head - py),
    ])
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPolygon(tri)


def _text_box(p: QPainter, a: Annotation, p1: QPointF, p2: QPointF, w: float, h: float) -> None:
    rect = QRectF(p1, p2).normalized()
    if rect.width() < w * 0.03 or rect.height() < h * 0.03:
        rect = QRectF(p1.x(), p1.y(), w * 0.3, h * 0.1)
    bg = QColor(a.color)
    bg.setAlpha(225)
    radius = min(rect.height(), rect.width()) * 0.12
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(rect, radius, radius)

    if not a.text:
        return
    pad = h * 0.012
    inner = rect.adjusted(pad, pad, -pad, -pad)
    flags = int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap)
    font = QFont(FONT_FAMILY)
    font.setBold(True)
    # Testo più grande possibile che stia nel riquadro.
    size = int(min(h * 0.06, inner.height()))
    while size > 8:
        font.setPixelSize(size)
        br = QFontMetricsF(font).boundingRect(inner, flags, a.text)
        if br.width() <= inner.width() + 0.5 and br.height() <= inner.height() + 0.5:
            break
        size -= 1
    p.setFont(font)
    p.setPen(_contrast(QColor(a.color)))
    p.drawText(inner, flags, a.text)


def draw_cover(p: QPainter, c: Cover, w: float, h: float) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.fillRect(QRectF(0, 0, w, h), QColor(c.bg_color))
    text_color = QColor(c.text_color)
    flags = int(Qt.AlignmentFlag.AlignHCenter) | int(Qt.TextFlag.TextWordWrap)
    margin = w * 0.08

    title_font = QFont(FONT_FAMILY)
    title_font.setBold(True)
    title_font.setPixelSize(int(h * 0.09))
    sub_font = QFont(FONT_FAMILY)
    sub_font.setPixelSize(int(h * 0.045))

    area = QRectF(margin, 0, w - 2 * margin, h)
    title_br = QFontMetricsF(title_font).boundingRect(area, flags, c.title)
    sub_br = QRectF()
    if c.subtitle:
        sub_br = QFontMetricsF(sub_font).boundingRect(area, flags, c.subtitle)
    gap = h * 0.05 if c.subtitle else 0
    total = title_br.height() + gap + sub_br.height()
    y = (h - total) / 2

    p.setPen(text_color)
    p.setFont(title_font)
    p.drawText(QRectF(margin, y, w - 2 * margin, title_br.height()), flags, c.title)
    if c.subtitle:
        # Linea d'accento tra titolo e sottotitolo
        line_y = y + title_br.height() + gap / 2
        p.fillRect(QRectF(w / 2 - w * 0.05, line_y - h * 0.003, w * 0.1, h * 0.006), text_color)
        p.setFont(sub_font)
        p.drawText(QRectF(margin, y + title_br.height() + gap, w - 2 * margin, sub_br.height()),
                   flags, c.subtitle)
    p.restore()


def render_annotation_png(a: Annotation, w: int, h: int, path: str) -> None:
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    draw_annotation(p, a, w, h)
    p.end()
    img.save(path)


def render_cover_png(c: Cover, w: int, h: int, path: str) -> None:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(img)
    draw_cover(p, c, w, h)
    p.end()
    img.save(path)
