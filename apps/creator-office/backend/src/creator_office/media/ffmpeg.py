"""FFmpeg wrapper: probing, signal analysis, frame extraction and rendering.

Everything here shells out to a real FFmpeg binary. When none is available
``locate()`` returns ``None`` and callers surface that instead of pretending.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from creator_office.settings import PACKAGE_ROOT, get_settings

FONTS_DIR = PACKAGE_ROOT / "assets" / "fonts"

ProgressFn = Callable[[float], None]


class FFmpegError(RuntimeError):
    pass


class Cancelled(RuntimeError):  # noqa: N818 - control-flow signal, not an error
    pass


@dataclass(frozen=True)
class FFmpegInfo:
    path: str
    version: str
    source: str  # "FFMPEG_PATH" | "PATH" | "imageio-ffmpeg"


@lru_cache
def locate() -> FFmpegInfo | None:
    candidates: list[tuple[str, str]] = []
    configured = get_settings().ffmpeg_path
    if configured:
        candidates.append((configured, "FFMPEG_PATH"))
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append((on_path, "PATH"))
    try:
        import imageio_ffmpeg

        candidates.append((imageio_ffmpeg.get_ffmpeg_exe(), "imageio-ffmpeg"))
    except Exception:  # noqa: S110 - optional dependency, any failure means "not bundled"
        pass
    for path, source in candidates:
        try:
            out = subprocess.run(  # noqa: S603 - fixed arguments, the binary comes from the configuration
                [path, "-version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"ffmpeg version (\S+)", out.stdout)
        if out.returncode == 0 and match:
            return FFmpegInfo(path=path, version=match.group(1), source=source)
    return None


def require() -> FFmpegInfo:
    info = locate()
    if info is None:
        raise FFmpegError("FFmpeg no está disponible en este servidor.")
    return info


# --- Process runner ----------------------------------------------------------


def _run(
    args: list[str],
    *,
    duration: float | None = None,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    cwd: str | None = None,
) -> str:
    """Run FFmpeg, report progress from ``-progress`` and honour cancellation."""
    info = require()
    cmd = [
        info.path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-nostats",
        "-progress",
        "pipe:1",
        *args,
    ]
    process = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        errors="replace",
    )
    stderr_chunks: list[str] = []

    def _drain() -> None:
        assert process.stderr is not None  # noqa: S101
        for line in process.stderr:
            stderr_chunks.append(line)
            if len(stderr_chunks) > 400:
                del stderr_chunks[:200]

    drain = threading.Thread(target=_drain, daemon=True)
    drain.start()
    assert process.stdout is not None  # noqa: S101
    try:
        for line in process.stdout:
            if cancel is not None and cancel.is_set():
                process.kill()
                raise Cancelled("Tarea cancelada.")
            if on_progress and duration and line.startswith("out_time_us="):
                value = line.split("=", 1)[1].strip()
                if value.lstrip("-").isdigit():
                    on_progress(max(0.0, min(1.0, int(value) / 1_000_000 / duration)))
        process.wait()
    finally:
        if process.poll() is None:
            process.kill()
        drain.join(timeout=5)
    stderr = "".join(stderr_chunks)
    if process.returncode != 0:
        tail = "\n".join(stderr.strip().splitlines()[-6:])
        raise FFmpegError(f"FFmpeg terminó con error ({process.returncode}): {tail}")
    return stderr


# --- Probe -------------------------------------------------------------------

_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?[, ](\d{2,5})x(\d{2,5})")
_FPS = re.compile(r"([\d.]+) fps")
_TBR = re.compile(r"([\d.]+) tbr")
_AUDIO = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+)[^,]*, (\d+) Hz, ([^,\n]+)")
_ROTATION = re.compile(r"rotation of (-?[\d.]+) degrees")


def probe(path: Path) -> dict[str, Any]:
    """Read container facts from ``ffmpeg -i`` (no ffprobe needed)."""
    info = require()
    out = subprocess.run(  # noqa: S603
        [info.path, "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        errors="replace",
    )
    text = out.stderr
    if "Invalid data found" in text or "No such file" in text or "Input #0" not in text:
        raise FFmpegError("El archivo no se reconoce como vídeo o audio válido.")
    result: dict[str, Any] = {"duration": None, "video": None, "audio": None}
    match = _DURATION.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        result["duration"] = round(
            int(hours) * 3600 + int(minutes) * 60 + float(seconds), 3
        )
    for line in text.splitlines():
        if result["video"] is None and (video := _VIDEO.search(line)):
            if "attached pic" in line:
                continue
            fps_match = _FPS.search(line) or _TBR.search(line)
            result["video"] = {
                "codec": video.group(1),
                "width": int(video.group(2)),
                "height": int(video.group(3)),
                "fps": round(float(fps_match.group(1)), 3) if fps_match else None,
            }
        elif result["audio"] is None and (audio := _AUDIO.search(line)):
            result["audio"] = {
                "codec": audio.group(1),
                "sample_rate": int(audio.group(2)),
                "channels": audio.group(3).strip(),
            }
    rotation = _ROTATION.search(text)
    if (
        result["video"]
        and rotation
        and abs(round(float(rotation.group(1)))) % 180 == 90
    ):
        # Phone footage is stored sideways with a rotation flag; FFmpeg applies
        # it when decoding, so the displayed size is the swapped one.
        video = result["video"]
        video["width"], video["height"] = video["height"], video["width"]
    return result


def extract_frame(
    src: Path, at: float, dest: Path, *, width: int | None = None
) -> None:
    filters = [f"scale={width}:-2"] if width else []
    args = ["-ss", f"{max(0.0, at):.3f}", "-i", str(src), "-frames:v", "1"]
    if filters:
        args += ["-vf", ",".join(filters)]
    args += ["-q:v", "3", str(dest)]
    _run(args)
    if not dest.exists() or dest.stat().st_size == 0:
        raise FFmpegError("No se pudo extraer el fotograma solicitado.")


# --- Signal analysis ---------------------------------------------------------

FRAME_SECONDS = 0.25  # loudness resolution


@dataclass
class Signals:
    duration: float
    has_audio: bool
    loudness: list[float] = field(default_factory=list)  # RMS dBFS per FRAME_SECONDS
    silences: list[tuple[float, float]] = field(default_factory=list)
    scene_cuts: list[float] = field(default_factory=list)
    dynamic_range_db: float = 0.0
    quiet_threshold_db: float | None = None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _parse_metadata_file(path: Path, key: str) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    time = 0.0
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("frame:"):
            match = re.search(r"pts_time:([\d.]+)", line)
            time = float(match.group(1)) if match else time
        elif line.startswith(key + "="):
            raw = line.split("=", 1)[1].strip()
            try:
                rows.append((time, float(raw)))
            except ValueError:
                rows.append((time, -120.0))  # "-inf" for digital silence
    return rows


def analyze(
    src: Path,
    *,
    duration: float,
    has_audio: bool,
    has_video: bool,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> Signals:
    """Measure loudness, pauses and scene changes of a file."""
    signals = Signals(duration=duration, has_audio=has_audio)
    audio_share = 0.25 if has_video else 1.0
    with tempfile.TemporaryDirectory(prefix="co-analyze-") as tmp:
        if has_audio:
            samples = int(16000 * FRAME_SECONDS)
            _run(
                [
                    "-i",
                    str(src),
                    "-vn",
                    "-af",
                    f"aresample=16000,asetnsamples=n={samples}:p=0,astats=metadata=1:reset=1,"
                    "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=loudness.txt",
                    "-f",
                    "null",
                    "-",
                ],
                duration=duration,
                on_progress=(lambda f: on_progress(f * audio_share))
                if on_progress
                else None,
                cancel=cancel,
                cwd=tmp,
            )
            rows = _parse_metadata_file(
                Path(tmp) / "loudness.txt", "lavfi.astats.Overall.RMS_level"
            )
            signals.loudness = [max(-90.0, value) for _, value in rows]
            if signals.loudness:
                low, high = (
                    _percentile(signals.loudness, 0.2),
                    _percentile(signals.loudness, 0.9),
                )
                signals.dynamic_range_db = round(high - low, 1)
                if signals.dynamic_range_db >= 6.0:
                    threshold = low + 0.35 * (high - low)
                    signals.quiet_threshold_db = round(threshold, 1)
                    signals.silences = _quiet_runs(signals.loudness, threshold)
        if has_video:
            _run(
                [
                    "-i",
                    str(src),
                    "-an",
                    "-vf",
                    "fps=4,scale=160:-2,select='gt(scene,0.30)',"
                    "metadata=print:key=lavfi.scene_score:file=scenes.txt",
                    "-f",
                    "null",
                    "-",
                ],
                duration=duration,
                on_progress=(lambda f: on_progress(audio_share + f * (1 - audio_share)))
                if on_progress
                else None,
                cancel=cancel,
                cwd=tmp,
            )
            signals.scene_cuts = [
                round(t, 2)
                for t, _ in _parse_metadata_file(
                    Path(tmp) / "scenes.txt", "lavfi.scene_score"
                )
            ]
    return signals


def _quiet_runs(
    loudness: list[float], threshold: float, min_seconds: float = 0.5
) -> list[tuple[float, float]]:
    runs: list[tuple[float, float]] = []
    start: int | None = None
    for index, value in enumerate([*loudness, 0.0]):
        quiet = index < len(loudness) and value < threshold
        if quiet and start is None:
            start = index
        elif not quiet and start is not None:
            if (index - start) * FRAME_SECONDS >= min_seconds:
                runs.append(
                    (round(start * FRAME_SECONDS, 2), round(index * FRAME_SECONDS, 2))
                )
            start = None
    return runs


# --- Rendering ---------------------------------------------------------------

FRAMINGS = ("crop", "blur", "pad")


def _video_chain(
    target: tuple[int, int],
    framing: str,
    offset_x: float,
    offset_y: float,
    subtitles: bool,
) -> str:
    width, height = target
    ox = min(1.0, max(0.0, offset_x))
    oy = min(1.0, max(0.0, offset_y))
    if framing == "blur":
        chain = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"boxblur=luma_radius=24:luma_power=2[bg];"
            f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    elif framing == "pad":
        chain = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    else:
        chain = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(iw-{width})*{ox:.4f}:(ih-{height})*{oy:.4f}"
        )
    chain += ",setsar=1"
    if subtitles:
        chain += ",subtitles=subs.ass:fontsdir=fonts"
    return chain + ",format=yuv420p[v]"


def render(
    src: Path,
    dest: Path,
    *,
    start: float,
    end: float,
    target: tuple[int, int],
    framing: str = "crop",
    offset_x: float = 0.5,
    offset_y: float = 0.5,
    ass_subtitles: str | None = None,
    has_audio: bool = True,
    fps: float | None = None,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Cut ``[start, end]`` from ``src`` and export an H.264/AAC MP4 at ``target`` size."""
    if framing not in FRAMINGS:
        raise FFmpegError(f"Encuadre desconocido: {framing}")
    duration = max(0.1, end - start)
    with tempfile.TemporaryDirectory(prefix="co-render-") as tmp:
        workdir = Path(tmp)
        if ass_subtitles:
            (workdir / "subs.ass").write_text(ass_subtitles, encoding="utf-8")
            shutil.copytree(FONTS_DIR, workdir / "fonts")
        args = [
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(src),
            "-filter_complex",
            _video_chain(target, framing, offset_x, offset_y, bool(ass_subtitles)),
            "-map",
            "[v]",
        ]
        if has_audio:
            args += [
                "-map",
                "0:a:0",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
                "-ac",
                "2",
            ]
        if fps and fps > 60:
            args += ["-r", "60"]
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-profile:v",
            "high",
            "-movflags",
            "+faststart",
            str(dest.resolve()),
        ]
        _run(args, duration=duration, on_progress=on_progress, cancel=cancel, cwd=tmp)
    if not dest.exists() or dest.stat().st_size == 0:
        raise FFmpegError("FFmpeg no generó el archivo de salida.")


def extract_audio(
    src: Path, dest: Path, *, cancel: threading.Event | None = None
) -> None:
    """Mono 16 kHz MP3, small enough to send to a transcription API."""
    _run(
        [
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            str(dest.resolve()),
        ],
        cancel=cancel,
    )
