"""Export video tramite FFmpeg.

Ogni clip viene tagliata e ricodificata con le annotazioni sovrapposte
(una PNG trasparente per annotazione, attivata solo nel suo intervallo).
Le copertine diventano brevi video da un'immagine fissa. Tutti i pezzi
sono codificati con gli stessi parametri, così si possono unire con
il concat demuxer senza ricodificare.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from .model import Clip, Cover, Item, Project, item_duration, item_name
from .render import render_annotation_png, render_cover_png

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class ExportCancelled(Exception):
    pass


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError("FFmpeg non trovato.")


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    has_audio: bool
    duration: float


DEFAULT_INFO = VideoInfo(1920, 1080, 25.0, False, 0.0)


def probe(path: str) -> VideoInfo:
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path],
                       capture_output=True, text=True, errors="replace",
                       creationflags=_NO_WINDOW)
    err = r.stderr
    size = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", err)
    if not size:
        raise RuntimeError(f"Impossibile leggere il video:\n{err[-800:]}")
    fps = re.search(r"Video:.*?(\d+(?:\.\d+)?) fps", err) or re.search(r"Video:.*?(\d+(?:\.\d+)?) tbr", err)
    dur = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    duration = 0.0
    if dur:
        duration = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3))
    return VideoInfo(
        width=int(size.group(1)),
        height=int(size.group(2)),
        fps=float(fps.group(1)) if fps else 25.0,
        has_audio=bool(re.search(r"Stream #.*Audio:", err)),
        duration=duration,
    )


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:80] or "clip"


class Exporter:
    def __init__(self, project: Project,
                 progress: Optional[Callable[[float], None]] = None,
                 is_cancelled: Optional[Callable[[], bool]] = None):
        self.project = project
        self.ffmpeg = ffmpeg_exe()
        self.progress = progress or (lambda f: None)
        self.is_cancelled = is_cancelled or (lambda: False)
        needs_video = any(isinstance(i, Clip) for i in project.items)
        if project.video_path and os.path.exists(project.video_path):
            self.info = probe(project.video_path)
        elif needs_video:
            raise RuntimeError("Il video della partita non è stato trovato.")
        else:
            self.info = DEFAULT_INFO
        self.W = self.info.width - self.info.width % 2
        self.H = self.info.height - self.info.height % 2
        self.fps = f"{min(self.info.fps, 60.0):.3f}".rstrip("0").rstrip(".")

    # ---- API pubblica -------------------------------------------------

    def export_combined(self, items: list[Item], out: str) -> None:
        """Tutti gli elementi, nell'ordine dato, in un unico file."""
        items = [i for i in items if item_duration(i) > 0]
        if not items:
            raise RuntimeError("Non c'è niente da esportare.")
        total = sum(item_duration(i) for i in items)
        with tempfile.TemporaryDirectory(prefix="hbv_") as tmp:
            if len(items) == 1:
                self._render_item(items[0], out, tmp, 0.0, 1.0)
            else:
                segments, done = [], 0.0
                for n, item in enumerate(items):
                    seg = os.path.join(tmp, f"seg_{n:03d}.mp4")
                    d = item_duration(item)
                    self._render_item(item, seg, tmp, 0.97 * done / total, 0.97 * d / total)
                    segments.append(seg)
                    done += d
                list_file = os.path.join(tmp, "list.txt")
                with open(list_file, "w", encoding="utf-8") as f:
                    for seg in segments:
                        f.write("file '{}'\n".format(seg.replace("'", "'\\''")))
                self._run(["-f", "concat", "-safe", "0", "-i", list_file,
                           "-c", "copy", "-movflags", "+faststart", out], 0, 0.97, 0.03)
        self.progress(1.0)

    def export_separately(self, items: list[Item], folder: str) -> list[str]:
        """Un file per elemento, numerati nell'ordine della sequenza."""
        items = [i for i in items if item_duration(i) > 0]
        total = sum(item_duration(i) for i in items) or 1.0
        outputs, done = [], 0.0
        with tempfile.TemporaryDirectory(prefix="hbv_") as tmp:
            for n, item in enumerate(items, 1):
                out = os.path.join(folder, f"{n:02d} - {safe_filename(item_name(item))}.mp4")
                d = item_duration(item)
                self._render_item(item, out, tmp, done / total, d / total)
                outputs.append(out)
                done += d
        self.progress(1.0)
        return outputs

    # ---- interni ------------------------------------------------------

    def _encode_args(self) -> list[str]:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-r", self.fps,
                "-video_track_timescale", "90000",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart"]

    def _render_item(self, item: Item, out: str, tmp: str, base: float, span: float) -> None:
        if isinstance(item, Cover):
            self._render_cover(item, out, tmp, base, span)
        else:
            self._render_clip(item, out, tmp, base, span)

    def _render_clip(self, clip: Clip, out: str, tmp: str, base: float, span: float) -> None:
        W, H, dur = self.W, self.H, clip.duration
        d = f"{dur:.3f}"
        args = ["-ss", f"{clip.start:.3f}", "-t", d, "-i", self.project.video_path]
        anns = [a for a in clip.annotations if a.start < dur]
        for a in anns:
            png = os.path.join(tmp, f"{clip.id}_{a.id}.png")
            render_annotation_png(a, W, H, png)
            args += ["-loop", "1", "-t", d, "-i", png]
        if self.info.has_audio:
            audio_map = "0:a:0"
        else:
            args += ["-f", "lavfi", "-t", d, "-i", "anullsrc=r=48000:cl=stereo"]
            audio_map = f"{len(anns) + 1}:a"

        graph = [f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                 f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.fps}[v0]"]
        for i, a in enumerate(anns, 1):
            graph.append(f"[v{i - 1}][{i}:v]overlay=0:0:"
                         f"enable='between(t,{a.start:.3f},{a.end_in(dur):.3f})'[v{i}]")
        args += ["-filter_complex", ";".join(graph),
                 "-map", f"[v{len(anns)}]", "-map", audio_map,
                 *self._encode_args(), "-t", d, out]
        self._run(args, dur, base, span)

    def _render_cover(self, cover: Cover, out: str, tmp: str, base: float, span: float) -> None:
        png = os.path.join(tmp, f"cover_{cover.id}.png")
        render_cover_png(cover, self.W, self.H, png)
        d = f"{cover.duration:.3f}"
        args = ["-loop", "1", "-framerate", self.fps, "-t", d, "-i", png,
                "-f", "lavfi", "-t", d, "-i", "anullsrc=r=48000:cl=stereo",
                "-vf", "setsar=1", "-map", "0:v", "-map", "1:a",
                *self._encode_args(), "-t", d, out]
        self._run(args, cover.duration, base, span)

    def _run(self, args: list[str], duration: float, base: float, span: float) -> None:
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-nostats", "-progress", "pipe:1", *args]
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                    stdin=subprocess.DEVNULL, text=True, errors="replace",
                                    creationflags=_NO_WINDOW)
            assert proc.stdout is not None
            for line in proc.stdout:
                if self.is_cancelled():
                    proc.kill()
                    proc.wait()
                    raise ExportCancelled()
                if duration and line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        secs = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    self.progress(base + span * min(1.0, secs / duration))
            rc = proc.wait()
            if self.is_cancelled():
                raise ExportCancelled()
            if rc != 0:
                errf.seek(0)
                raise RuntimeError("FFmpeg ha restituito un errore:\n" + errf.read()[-1500:])
        self.progress(base + span)
