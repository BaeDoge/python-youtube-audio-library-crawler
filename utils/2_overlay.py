#!/usr/bin/env python3
"""now Playing Korea - Shorts PNG Overlay"""

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

try:
    PNG_OVERLAY_ALPHA = float(os.getenv("PNG_OVERLAY_ALPHA", "0.65"))
    PNG_OVERLAY_Y = float(os.getenv("PNG_OVERLAY_Y", "0.70"))
    PNG_OVERLAY_SCALE = float(os.getenv("PNG_OVERLAY_SCALE", "0.50"))
except ValueError:
    print("❌ .env의 PNG_OVERLAY_* 값은 숫자여야 합니다.")
    raise SystemExit(1)


def decode_output(data: bytes) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def run_command(args):
    # text=True 금지: Windows cp949 디코딩 오류 방지
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
        mp4_count = sum(
            1 for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".mp4"
        )
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
    print()
    print("텍스트 PNG 파일 경로를 입력하세요.")
    print('예: C:\\Users\\qold0\\Desktop\\jeju_text.png')
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


def validate_settings():
    global PNG_OVERLAY_ALPHA, PNG_OVERLAY_Y, PNG_OVERLAY_SCALE

    if not 0 <= PNG_OVERLAY_ALPHA <= 1:
        print("⚠ PNG_OVERLAY_ALPHA는 0~1 범위여야 합니다. 0.65로 사용합니다.")
        PNG_OVERLAY_ALPHA = 0.65
    if not 0 <= PNG_OVERLAY_Y <= 1:
        print("⚠ PNG_OVERLAY_Y는 0~1 범위여야 합니다. 0.70으로 사용합니다.")
        PNG_OVERLAY_Y = 0.70
    if PNG_OVERLAY_SCALE <= 0:
        print("⚠ PNG_OVERLAY_SCALE은 0보다 커야 합니다. 0.50으로 사용합니다.")
        PNG_OVERLAY_SCALE = 0.50


def create_overlay_video(source: Path, png: Path, output: Path):
    filter_complex = (
        "[1:v]format=rgba,"
        f"scale=iw*{PNG_OVERLAY_SCALE}:ih*{PNG_OVERLAY_SCALE},"
        f"colorchannelmixer=aa={PNG_OVERLAY_ALPHA}[png];"
        "[0:v][png]overlay="
        "x=(main_w-overlay_w)/2:"
        f"y=main_h*{PNG_OVERLAY_Y}-overlay_h/2:"
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
        error = decode_output(result.stderr).strip()
        print(error if error else "(ffmpeg stderr가 비어 있습니다.)")
        return False
    return True


def main():
    check_command("ffmpeg")
    validate_settings()

    folder = choose_directory()
    if folder is None:
        return

    png = choose_png()
    if png is None:
        return

    videos = sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"],
        key=lambda p: p.name,
    )
    if not videos:
        print("❌ 선택한 폴더에 MP4 영상이 없습니다.")
        return

    output_dir = folder / "overlay"
    output_dir.mkdir(exist_ok=True)

    print()
    print(f"🎨 PNG: {png}")
    print(f"📁 원본 폴더: {folder}")
    print(f"📁 결과 폴더: {output_dir}")
    print(f"🔳 투명도: {PNG_OVERLAY_ALPHA}")
    print(f"📍 PNG 중심 Y: 화면의 {PNG_OVERLAY_Y:.0%}")
    print(f"📏 PNG 크기: 원본의 {PNG_OVERLAY_SCALE:.0%}")
    print()

    success_count = 0
    for i, source in enumerate(videos, 1):
        output = output_dir / f"{source.stem}_text.mp4"
        print(f"[{i}/{len(videos)}] {source.name}")
        if create_overlay_video(source, png, output):
            print(f"    ✅ {output.name}")
            success_count += 1
        else:
            print("    ❌ 실패")

    print()
    print("=" * 70)
    print(f"완료: {success_count}/{len(videos)}개")
    print(f"결과: {output_dir}")
    print("=" * 70)


if __name__ == "__main__":
    main()
