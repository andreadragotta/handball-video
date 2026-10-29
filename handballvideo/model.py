"""Modello dati del progetto: clip, copertine e annotazioni.

Le coordinate delle annotazioni sono normalizzate (0..1) rispetto al
fotogramma, così restano valide a qualsiasi risoluzione di export.
I tempi delle annotazioni sono relativi all'inizio della clip.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional, Union

PROJECT_EXT = ".hvproj"

TOOL_LABELS = {
    "arrow": "Freccia",
    "pass": "Passaggio",
    "line": "Linea",
    "rect": "Riquadro",
    "ellipse": "Cerchio",
    "text": "Testo",
}


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Annotation:
    kind: str
    x1: float
    y1: float
    x2: float
    y2: float
    color: str = "#FFD400"
    width: float = 0.007  # spessore relativo all'altezza del fotogramma
    text: str = ""
    start: float = 0.0
    end: Optional[float] = None  # None = fino alla fine della clip
    id: str = field(default_factory=_new_id)

    def end_in(self, clip_duration: float) -> float:
        return clip_duration if self.end is None else min(self.end, clip_duration)

    def visible_at(self, t: float, clip_duration: float) -> bool:
        return self.start <= t <= self.end_in(clip_duration)


@dataclass
class Clip:
    name: str
    start: float
    end: float
    annotations: list[Annotation] = field(default_factory=list)
    tag: str = ""
    id: str = field(default_factory=_new_id)
    type: str = "clip"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Cover:
    title: str = "Titolo"
    subtitle: str = ""
    bg_color: str = "#0B3D91"
    text_color: str = "#FFFFFF"
    duration: float = 3.0
    id: str = field(default_factory=_new_id)
    type: str = "cover"


Item = Union[Clip, Cover]


def item_duration(item: Item) -> float:
    return item.duration


def item_name(item: Item) -> str:
    return item.name if isinstance(item, Clip) else (item.title or "Copertina")


@dataclass
class Project:
    video_path: str = ""
    items: list = field(default_factory=list)
    version: int = 1

    def find(self, item_id: str) -> Optional[Item]:
        return next((i for i in self.items if i.id == item_id), None)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        data = json.loads(json.dumps(data))  # copia profonda: from_dict modifica i dict
        items: list = []
        for d in data.get("items", []):
            if d.get("type") == "cover":
                items.append(Cover(**d))
            else:
                anns = [Annotation(**a) for a in d.pop("annotations", [])]
                items.append(Clip(**d, annotations=anns))
        return cls(video_path=data.get("video_path", ""), items=items,
                   version=data.get("version", 1))


def fmt_time(sec: float, precise: bool = False) -> str:
    sec = max(0.0, sec or 0.0)
    if precise:
        cs = int(round(sec * 100))
        h, rem = divmod(cs, 360000)
        m, rem = divmod(rem, 6000)
        s, c = divmod(rem, 100)
        return f"{h}:{m:02d}:{s:02d}.{c:02d}" if h else f"{m}:{s:02d}.{c:02d}"
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str) -> Optional[float]:
    """Accetta '83.5', '1:23.5', '1:02:03'."""
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    if len(parts) > 3 or any(p < 0 for p in parts):
        return None
    value = 0.0
    for p in parts:
        value = value * 60 + p
    return value
