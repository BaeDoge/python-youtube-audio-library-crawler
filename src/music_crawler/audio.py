"""Thin ffmpeg/ffprobe wrappers: probing, hashing and rendering a crossfaded playlist."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from music_crawler.config import Settings

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}

# magic bytes of the containers we accept; guards against HTML error pages saved as .mp3
_SIGNATURES = (
    b"ID3",
    b"\xff\xfb",
    b"\xff\xf3",
    b"\xff\xf2",
    b"\xff\xe3",
    b"OggS",
    b"fLaC",
    b"RIFF",
    b"\xff\xf1",
    b"\xff\xf9",
)


class AudioError(RuntimeError):
    pass


def which(binary: str) -> str | None:
    return shutil.which(binary)


def looks_like_audio(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    return head.startswith(_SIGNATURES) or head[4:8] == b"ftyp"


def file_hash(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(settings: Settings, path: Path) -> dict:
    """Return ``{"duration": int seconds, "tags": {lower-case tag: value}}``; raises AudioError."""
    ffprobe = which(settings.ffprobe_bin)
    if not ffprobe:
        raise AudioError("ffprobe를 찾을 수 없습니다 (FFPROBE_BIN 설정 또는 PATH 확인).")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:format_tags",
        "-of",
        "json",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout
        fmt = json.loads(out).get("format") or {}
        duration = float(fmt.get("duration") or 0)
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        raise AudioError(f"ffprobe 실패: {path.name}: {exc}") from exc
    if duration <= 0:
        raise AudioError(f"재생 시간을 읽을 수 없습니다: {path.name}")
    tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
    return {"duration": int(round(duration)), "tags": tags}


@dataclass
class RenderItem:
    path: Path
    duration: float
    trim_to: float | None = None  # cut + fade out (only meaningful for the last item)


def build_filter(items: list[RenderItem], crossfade: float, normalize: bool) -> str:
    """Build the ffmpeg ``-filter_complex`` graph (exposed for unit tests)."""
    parts: list[str] = []
    for i, item in enumerate(items):
        chain = ["aresample=44100", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if normalize:
            chain += ["loudnorm=I=-16:TP=-1.5:LRA=11", "aresample=44100"]
        if item.trim_to and item.trim_to < item.duration:
            fade = min(5.0, item.trim_to / 4)
            chain += [
                f"atrim=duration={item.trim_to:.3f}",
                "asetpts=PTS-STARTPTS",
                f"afade=t=out:st={item.trim_to - fade:.3f}:d={fade:.3f}",
            ]
        parts.append(f"[{i}:a]{','.join(chain)}[a{i}]")
    if len(items) == 1:
        parts.append("[a0]anull[out]")
    elif crossfade > 0:
        prev = "a0"
        for i in range(1, len(items)):
            label = "out" if i == len(items) - 1 else f"x{i}"
            parts.append(f"[{prev}][a{i}]acrossfade=d={crossfade}:c1=tri:c2=tri[{label}]")
            prev = label
    else:
        joined = "".join(f"[a{i}]" for i in range(len(items)))
        parts.append(f"{joined}concat=n={len(items)}:v=0:a=1[out]")
    return ";".join(parts)


def render(
    settings: Settings,
    items: list[RenderItem],
    output: Path,
    *,
    crossfade: float | None = None,
    normalize: bool | None = None,
    title: str = "Background Music",
) -> Path:
    """Concatenate ``items`` into one MP3 with optional crossfade + loudness normalisation."""
    ffmpeg = which(settings.ffmpeg_bin)
    if not ffmpeg:
        raise AudioError("ffmpeg를 찾을 수 없습니다 (FFMPEG_BIN 설정 또는 PATH 확인).")
    if not items:
        raise AudioError("합칠 음원이 없습니다.")
    xfade = settings.crossfade_seconds if crossfade is None else crossfade
    norm_on = settings.normalize_loudness if normalize is None else normalize
    for item in items:
        if not item.path.exists():
            raise AudioError(f"파일이 없습니다: {item.path}")

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for item in items:
        cmd += ["-i", str(item.path)]
    cmd += [
        "-filter_complex",
        build_filter(items, xfade, norm_on),
        "-map",
        "[out]",
        "-c:a",
        "libmp3lame",
        "-b:a",
        settings.mp3_bitrate,
        "-id3v2_version",
        "3",
        "-metadata",
        f"title={title}",
        str(output),
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    total = sum(i.duration for i in items)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=max(600, int(total)))
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffmpeg 시간 초과") from exc
    if result.returncode != 0:
        output.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg 실패:\n{result.stderr[-1500:]}")
    return output
