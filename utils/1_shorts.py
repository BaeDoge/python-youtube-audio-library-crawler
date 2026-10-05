#!/usr/bin/env python3
"""now Playing Korea - Shorts 9:16 Clip Generator"""

import os
import re
import shutil
import subprocess
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    print("❌ python-dotenv가 설치되어 있지 않습니다.")
    print("설치: pip install python-dotenv")
    raise SystemExit(1)

# .env 위치: utils/.env 또는 프로젝트 루트/.env 모두 지원
SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env", override=False)
load_dotenv(SCRIPT_DIR.parent / ".env", override=False)

DEFAULT_SOURCE_DIR = Path(r"C:\Users\qold0\AppData\Local\CapCut\Videos")
SOURCE_DIR = Path(os.getenv("SHORTS_SOURCE_DIR", str(DEFAULT_SOURCE_DIR)))

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def decode_output(data: bytes) -> str:
    """Windows cp949 문제를 피하기 위해 subprocess 결과를 직접 UTF-8로 디코딩."""
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def run_command(args):
    # 중요: text=True를 사용하지 않는다.
    # Windows 기본 인코딩(cp949)으로 ffmpeg UTF-8 출력이 디코딩되는 문제를 방지한다.
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )


def check_command(command: str):
    if shutil.which(command) is None:
        print(f"❌ '{command}'을(를) 찾을 수 없습니다.")
        print("ffmpeg를 설치하고 PATH에 추가한 뒤 다시 실행해주세요.")
        raise SystemExit(1)


def parse_time(value: str) -> float:
    value = value.strip()
    parts = value.split(":")

    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass

    raise ValueError(f"잘못된 시간 형식: {value}")


def format_time(seconds: float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60

    if h:
        return f"{h:02d}-{m:02d}-{s:02d}"
    return f"{m:02d}-{s:02d}"


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(".") or "Untitled"


def get_duration(video: Path) -> float:
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video),
    ])

    if result.returncode != 0:
        error = decode_output(result.stderr).strip()
        raise RuntimeError(error or "ffprobe 실행 실패")

    try:
        return float(decode_output(result.stdout).strip())
    except ValueError as exc:
        raise RuntimeError("ffprobe가 올바른 영상 길이를 반환하지 않았습니다.") from exc


def list_videos():
    if not SOURCE_DIR.exists():
        print(f"❌ 영상 폴더가 없습니다: {SOURCE_DIR}")
        print("환경변수 SHORTS_SOURCE_DIR로 다른 폴더를 지정할 수 있습니다.")
        raise SystemExit(1)

    videos = [
        p for p in SOURCE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    return sorted(videos, key=lambda p: p.stat().st_mtime, reverse=True)


def create_vertical_clip(source: Path, output: Path, start: float, duration: float):
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920:(iw-1080)/2:(ih-1920)/2,"
        "setsar=1"
    )

    result = run_command([
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", str(source),
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(output),
    ])

    if result.returncode != 0:
        print("❌ ffmpeg 오류:")
        error = decode_output(result.stderr).strip()
        print(error if error else "(ffmpeg stderr가 비어 있습니다.)")
        return False

    return True


def main():
    check_command("ffmpeg")
    check_command("ffprobe")

    videos = list_videos()
    if not videos:
        print(f"❌ 영상 파일이 없습니다: {SOURCE_DIR}")
        return

    print("=" * 70)
    print(" now Playing Korea - 9:16 Shorts Generator")
    print("=" * 70)
    print(f"영상 폴더: {SOURCE_DIR}")
    print()

    for i, video in enumerate(videos, 1):
        try:
            duration = get_duration(video)
            duration_text = f"{duration / 60:.1f}분"
        except Exception:
            duration_text = "길이 확인 실패"
        print(f"[{i:2}] {video.name} ({duration_text})")

    print()
    while True:
        choice = input("사용할 영상 번호를 선택하세요 (q: 종료): ").strip()
        if choice.lower() == "q":
            return
        try:
            index = int(choice) - 1
            if not 0 <= index < len(videos):
                raise ValueError
            source = videos[index]
            break
        except ValueError:
            print("❌ 올바른 번호를 입력해주세요.")

    try:
        source_duration = get_duration(source)
    except Exception as e:
        print(f"❌ 영상 길이를 확인할 수 없습니다: {e}")
        return

    title = input(f"\n영상 제목을 입력하세요 (Enter = {source.stem}): ").strip()
    title = sanitize_filename(title or source.stem)

    shorts_root = SOURCE_DIR.parent / "Shorts"
    output_dir = shorts_root / title
    output_dir.mkdir(parents=True, exist_ok=True)

    print()
    print(f"📁 저장 폴더: {output_dir}")
    print("📐 출력 형식: 1080x1920 (9:16), 중앙 크롭, 무음")
    print("💡 시작 시간은 90 / 01:30 / 00:01:30 형식 모두 가능합니다.")
    print("💡 종료하려면 시작 시간 입력에서 q를 입력하세요.")
    print()

    next_number = 1
    existing = list(output_dir.glob("*.mp4"))
    numbers = []
    for file in existing:
        match = re.match(r"(\d+)_", file.name)
        if match:
            numbers.append(int(match.group(1)))
    if numbers:
        next_number = max(numbers) + 1

    while True:
        start_input = input("시작 시간 (q: 종료): ").strip()
        if start_input.lower() == "q":
            break

        try:
            start = parse_time(start_input)
        except ValueError as e:
            print(f"❌ {e}")
            continue

        if start < 0 or start >= source_duration:
            print(f"❌ 시작 시간은 0 ~ {source_duration:.1f}초 사이여야 합니다.")
            continue

        duration_input = input("길이 (Enter = 30초): ").strip()
        if not duration_input:
            duration = 30.0
        else:
            try:
                duration = parse_time(duration_input)
            except ValueError as e:
                print(f"❌ {e}")
                continue

        if duration <= 0:
            print("❌ 길이는 0보다 커야 합니다.")
            continue

        actual_duration = min(duration, source_duration - start)
        output_name = (
            f"{next_number:02d}_"
            f"{format_time(start)}_to_{format_time(start + actual_duration)}.mp4"
        )
        output = output_dir / output_name

        print(f"🎬 생성 중: {output.name}")
        if create_vertical_clip(source, output, start, actual_duration):
            print(f"✅ 완료: {output}")
            next_number += 1
        else:
            print("❌ 생성 실패")
        print()


if __name__ == "__main__":
    main()
