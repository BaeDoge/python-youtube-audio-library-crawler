#!/usr/bin/env python3
"""
now Playing Korea - Shorts PNG Overlay

- Select a folder under the Shorts directory.
- Select a PNG file.
- Apply the PNG to every MP4 in the selected folder.
- The PNG is centered horizontally and placed slightly below center.
- PNG is shown for the entire video.
- The PNG itself is made semi-transparent by the alpha setting below.
- Original videos are not modified; results go to an "overlay" subfolder.
- Requires ffmpeg and ffprobe on PATH.

PNG_OVERLAY_ALPHA:
    0.0 = invisible
    1.0 = fully opaque
    0.65 = recommended starting point
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

DEFAULT_SOURCE_DIR = Path(r"C:\Users\qold0\AppData\Local\CapCut\Videos")
SOURCE_DIR = Path(os.getenv("SHORTS_SOURCE_DIR", str(DEFAULT_SOURCE_DIR)))

PNG_EXTENSIONS = {".png"}
VIDEO_EXTENSIONS = {".mp4"}

# PNG 전체 투명도.
# 0.65 정도부터 시작해보고 취향에 따라 조절하세요.
PNG_OVERLAY_ALPHA = 0.65

# 화면 세로 위치.
# 0.55 = 정중앙보다 약간 아래
# 0.60 = 조금 더 아래
OVERLAY_CENTER_Y = 0.55


def check_command(command: str):
    if shutil.which(command) is None:
        print(f"❌ '{command}'을(를) 찾을 수 없습니다.")
        print("ffmpeg를 설치하고 PATH에 추가한 뒤 다시 실행해주세요.")
        raise SystemExit(1)


def run_command(args):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).rstrip(".") or "Untitled"


def choose_directory():
    shorts_root = SOURCE_DIR.parent / "Shorts"

    if not shorts_root.exists():
        print(f"❌ Shorts 폴더가 없습니다: {shorts_root}")
        print("먼저 1번 프로그램으로 Shorts 영상을 만들어주세요.")
        return None

    folders = sorted(
        [p for p in shorts_root.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not folders:
        print(f"❌ 제목 폴더가 없습니다: {shorts_root}")
        return None

    print("=" * 70)
    print(" now Playing Korea - Shorts PNG Overlay")
    print("=" * 70)
    print(f"Shorts 폴더: {shorts_root}")
    print()

    for i, folder in enumerate(folders, 1):
        mp4_count = len([
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ])
        print(f"[{i:2}] {folder.name} ({mp4_count}개 영상)")

    print()

    while True:
        choice = input("오버레이할 제목 폴더 번호를 선택하세요 (q: 종료): ").strip()

        if choice.lower() == "q":
            return None

        try:
            index = int(choice) - 1
            if 0 <= index < len(folders):
                return folders[index]
        except ValueError:
            pass

        print("❌ 올바른 번호를 입력해주세요.")


def choose_png():
    default_png_dir = Path.cwd()

    print()
    print("텍스트 PNG 파일 경로를 입력하세요.")
    print("예: C:\\Users\\qold0\\Desktop\\jeju_text.png")
    print()

    while True:
        value = input("PNG 경로 (q: 종료): ").strip().strip('"')

        if value.lower() == "q":
            return None

        png = Path(value).expanduser()

        if not png.is_file():
            print("❌ 파일이 없습니다.")
            continue

        if png.suffix.lower() != ".png":
            print("❌ PNG 파일만 사용할 수 있습니다.")
            continue

        return png


def create_overlay_video(source: Path, png: Path, output: Path):
    # The PNG is scaled down only when necessary.
    # iw/ih refer to the PNG dimensions.
    #
    # Main output is 1080x1920, matching program #1.
    #
    # overlay x:
    #   (main_w-overlay_w)/2 -> horizontal center
    #
    # overlay y:
    #   main_h*0.55 - overlay_h/2 -> slightly below vertical center
    #
    # format=rgba + colorchannelmixer=aa controls PNG opacity.
    filter_complex = (
        "[1:v]format=rgba,"
        f"colorchannelmixer=aa={PNG_OVERLAY_ALPHA}[png];"
        "[0:v][png]overlay="
        "x=(main_w-overlay_w)/2:"
        f"y=main_h*{OVERLAY_CENTER_Y}-overlay_h/2:"
        "shortest=1"
    )

    result = run_command([
        "ffmpeg",
        "-y",
        "-i", str(source),
        "-loop", "1",
        "-i", str(png),
        "-filter_complex", filter_complex,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        str(output),
    ])

    if result.returncode != 0:
        print("❌ ffmpeg 오류:")
        print(result.stderr)
        return False

    return True


def main():
    check_command("ffmpeg")

    folder = choose_directory()
    if folder is None:
        return

    png = choose_png()
    if png is None:
        return

    videos = sorted(
        [
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ],
        key=lambda p: p.name,
    )

    if not videos:
        print("❌ 선택한 폴더에 MP4 영상이 없습니다.")
        return

    # Keep originals untouched.
    # Exported videos go into <title folder>\overlay\
    output_dir = folder / "overlay"
    output_dir.mkdir(exist_ok=True)

    print()
    print(f"🎨 PNG: {png}")
    print(f"📁 원본 폴더: {folder}")
    print(f"📁 결과 폴더: {output_dir}")
    print(f"🔳 투명도: {PNG_OVERLAY_ALPHA}")
    print(f"📍 세로 위치: 화면의 {OVERLAY_CENTER_Y:.0%} 지점")
    print()

    for i, source in enumerate(videos, 1):
        output = output_dir / f"{source.stem}_text.mp4"

        print(f"[{i}/{len(videos)}] {source.name}")

        if create_overlay_video(source, png, output):
            print(f"    ✅ {output.name}")
        else:
            print("    ❌ 실패")

    print()
    print("=" * 70)
    print("완료!")
    print(f"결과: {output_dir}")
    print("=" * 70)


if __name__ == "__main__":
    main()
