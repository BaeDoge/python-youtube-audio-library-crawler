"""Command line interface: ``music-crawler <command>`` (alias ``mc``)."""

from __future__ import annotations

import argparse
import dataclasses
import random
import re
import sys
from collections import Counter
from pathlib import Path

from music_crawler import __version__, audio, legacy
from music_crawler import playlist as pl
from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.playlist import Playlist, fmt_time
from music_crawler.selector import Criteria, filter_candidates
from music_crawler.taxonomy import (
    GENRES,
    MOODS,
    load_profiles,
    normalize_genres,
    normalize_moods,
)

ENV_TEMPLATE = """\
# Copy of .env.example — fill in what you use. Never commit this file.
JAMENDO_CLIENT_ID=
OPENVERSE_CLIENT_ID=
OPENVERSE_CLIENT_SECRET=
YOUTUBE_CHANNEL_ID=
FFMPEG_BIN=ffmpeg
FFPROBE_BIN=ffprobe
"""


# ----------------------------------------------------------------------------- helpers
def parse_length(value: str) -> int:
    """``60`` → 60 min, ``45:00`` → 45 min, ``1:15:00`` → 75 min. Returns seconds."""
    parts = value.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(f"길이 형식이 올바르지 않습니다: {value!r}") from None
    if len(nums) == 1:
        seconds = nums[0] * 60
    elif len(nums) == 2:
        seconds = nums[0] * 60 + nums[1]
    elif len(nums) == 3:
        seconds = nums[0] * 3600 + nums[1] * 60 + nums[2]
    else:
        raise argparse.ArgumentTypeError(f"길이 형식이 올바르지 않습니다: {value!r}")
    if seconds < 60:
        raise argparse.ArgumentTypeError("길이는 1분 이상이어야 합니다.")
    return seconds


def _split(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _resolve_vocab(raw: str | None, normalizer, label: str, valid) -> tuple[str, ...] | None:
    """``None`` → not given; ``()`` → 'all'; else canonical names."""
    if raw is None:
        return None
    parts = _split(raw)
    if [p.lower() for p in parts] in (["all"], ["any"], ["전체"]):
        return ()
    out: list[str] = []
    for p in parts:
        mapped = normalizer([p])
        if not mapped:
            raise SystemExit(f"알 수 없는 {label}: {p!r}\n사용 가능: {', '.join(valid)}")
        out += [m for m in mapped if m not in out]
    return tuple(out)


def build_criteria(args, settings: Settings, profiles: dict) -> Criteria:
    profile = profiles.get(getattr(args, "profile", "relax"))
    if profile is None:
        raise SystemExit(f"없는 프로필: {args.profile} (사용 가능: {', '.join(profiles)})")
    genres = _resolve_vocab(getattr(args, "genre", None), normalize_genres, "장르", GENRES)
    moods = _resolve_vocab(getattr(args, "mood", None), normalize_moods, "mood", MOODS)
    return Criteria(
        genres=profile.genres if genres is None else genres,
        moods=profile.moods if moods is None else moods,
        vocals=getattr(args, "vocals", None) or profile.vocals,
        sources=tuple(_split(getattr(args, "source", None))),
        strict_mood=getattr(args, "strict_mood", False),
        exclude_used=not getattr(args, "allow_reuse", False),
        min_seconds=settings.min_track_seconds,
        max_seconds=settings.max_track_seconds,
    )


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h"


def _print_playlist(settings: Settings, db: Database, p: Playlist) -> None:
    xf = settings.crossfade_seconds
    length = p.length(xf)
    trim = p.trim_for_exact(xf)
    shown = p.target_seconds if trim else length
    print(f"\n{p.name}  [{p.id}]  상태: {p.status}")
    print(
        f"목표 {fmt_time(p.target_seconds)} / 현재 {fmt_time(shown)} "
        f"({'+' if shown >= p.target_seconds else '-'}{fmt_time(abs(shown - p.target_seconds))})"
        f"{'  · 마지막 곡을 잘라 정확히 맞춤' if trim else ''}"
    )
    print(f"{'#':>2}  {'시작':>7}  {'길이':>6}  {'곡':<48} {'출처':<9} {'라이선스':<16} 보컬")
    for n, (start, item) in enumerate(p.timeline(xf), 1):
        track = db.get(item.track_id)
        vocals = track.vocals if track else "?"
        label = f"{item.artist + ' - ' if item.artist else ''}{item.title}"
        lock = "🔒" if item.locked else "  "
        print(
            f"{n:>2}  {fmt_time(start):>7}  {fmt_time(item.duration):>6}  {label[:46]:<48} "
            f"{item.source:<9} {item.license:<16} {vocals} {lock}"
        )
    credits = p.credits()
    if credits:
        print(
            f"\n※ 저작자 표시가 필요한 곡 {len(credits)}곡 — description.txt 에 크레딧이 자동 포함됩니다."
        )


def _open_db(settings: Settings) -> Database:
    settings.ensure_dirs()
    return Database(settings.db_path)


# --------------------------------------------------------------------------- commands
def cmd_init(args, settings: Settings) -> int:
    settings.ensure_dirs()
    env = settings.home / ".env"
    if not env.exists():
        env.write_text(ENV_TEMPLATE, encoding="utf-8")
        print(f"생성: {env}")
    print(f"작업 폴더 준비 완료: {settings.home}")
    print(f"직접 받은 음원은 {settings.inbox_dir} 에 넣으세요 (예: inbox/Jazz/Calm/곡.mp3).")
    return 0


def cmd_doctor(args, settings: Settings) -> int:
    ok = True

    def line(flag: bool, text: str, required: bool = True) -> None:
        nonlocal ok
        print(("✅ " if flag else ("❌ " if required else "➖ ")) + text)
        ok = ok and (flag or not required)

    line(bool(audio.which(settings.ffmpeg_bin)), f"ffmpeg ({settings.ffmpeg_bin})")
    line(bool(audio.which(settings.ffprobe_bin)), f"ffprobe ({settings.ffprobe_bin})")
    line(bool(settings.jamendo_client_id), "JAMENDO_CLIENT_ID", required=False)
    line(
        bool(settings.openverse_client_id),
        "OPENVERSE_CLIENT_ID (없어도 익명으로 동작, 제한 낮음)",
        required=False,
    )
    line(
        bool(settings.youtube_channel_id),
        "YOUTUBE_CHANNEL_ID (YouTube 오디오 라이브러리용)",
        required=False,
    )
    try:
        import playwright  # noqa: F401

        line(True, "playwright (YouTube 오디오 라이브러리용)", required=False)
    except ImportError:
        line(
            False,
            "playwright 미설치 — YouTube 오디오 라이브러리를 쓰려면 pip install '.[youtube]'",
            required=False,
        )
    with _open_db(settings) as db:
        counts = db.count_by_source()
    print(f"\nDB: {settings.db_path}  후보 곡: {sum(counts.values()):,} {counts}")
    return 0 if ok else 1


def cmd_stats(args, settings: Settings) -> int:
    profiles = load_profiles(settings.profiles_file)
    criteria = build_criteria(args, settings, profiles)
    with _open_db(settings) as db:
        everything = db.all_tracks()
        pool = filter_candidates(db, criteria)
    print(f"전체 후보 {len(everything):,}곡 ({_hours(sum(t.duration for t in everything))})")
    by_src = Counter(t.source for t in everything)
    print("  출처별: " + ", ".join(f"{k} {v:,}" for k, v in by_src.most_common()))
    print(
        f"\n조건: 장르={list(criteria.genres) or '전체'} mood={list(criteria.moods) or '전체'} "
        f"보컬={criteria.vocals} 출처={list(criteria.sources) or '전체'}"
    )
    total = sum(t.duration for t in pool)
    print(f"조건에 맞는 곡 {len(pool):,}곡 ({_hours(total)})")
    if not pool:
        return 0
    for title, counter in (
        ("출처", Counter(t.source for t in pool)),
        ("장르", Counter(g for t in pool for g in t.genres)),
        ("mood", Counter(m for t in pool for m in (t.moods or ["(없음)"]))),
        ("보컬", Counter(t.vocals for t in pool)),
        ("라이선스", Counter(t.license for t in pool)),
    ):
        print(f"  {title}: " + ", ".join(f"{k} {v:,}" for k, v in counter.most_common(10)))
    for minutes in (30, 60, 120):
        print(
            f"  → {minutes}분짜리 플레이리스트를 곡 겹침 없이 약 {int(total // (minutes * 60))}개"
        )
    return 0


def cmd_sync(args, settings: Settings) -> int:
    profiles = load_profiles(settings.profiles_file)
    profile = profiles.get(args.profile)
    if profile is None:
        raise SystemExit(f"없는 프로필: {args.profile}")
    wanted = ["jamendo", "openverse", "local"] if args.source == "all" else [args.source]
    code = 0
    with _open_db(settings) as db:
        for name in wanted:
            print(f"\n== {name} ==")
            try:
                if name == "jamendo":
                    from music_crawler.providers import jamendo

                    if not settings.jamendo_client_id and args.source == "all":
                        print("JAMENDO_CLIENT_ID가 없어 건너뜁니다.")
                        continue
                    report = jamendo.sync(
                        settings,
                        db,
                        profile,
                        per_tag_limit=None if args.all else args.per_tag,
                        reset=args.reset,
                        progress=print,
                    )
                elif name == "openverse":
                    from music_crawler.providers import openverse

                    report = openverse.sync(
                        settings, db, profile, per_query_limit=args.per_query, progress=print
                    )
                elif name == "local":
                    from music_crawler.providers import local

                    report = local.sync(settings, db, Path(args.path) if args.path else None)
                elif name == "youtube":
                    from music_crawler.providers import youtube_library

                    report = youtube_library.sync(settings, db, profile, progress=print)
                else:
                    raise SystemExit(f"알 수 없는 출처: {name}")
            except Exception as exc:  # noqa: BLE001 - one failing source must not stop the rest
                print(f"❌ {name} 실패: {exc}")
                code = 1
                continue
            print("\n".join(report.lines()))
    print("\n`music-crawler stats` 로 후보 풀을 확인하세요.")
    return code


def pick_random_genres(
    pool, target_seconds: int, count: int, rng: random.Random
) -> tuple[str, ...]:
    """Choose ``count`` genres that each have enough music to carry their share of the length."""
    seconds: Counter = Counter()
    for t in pool:
        for g in set(t.genres):
            if g != "Other":
                seconds[g] += t.duration
    need = target_seconds * 1.3 / count
    eligible = sorted(g for g, sec in seconds.items() if sec >= need)
    if len(eligible) < count:
        raise SystemExit(
            f"{count}개 장르를 채울 만큼 곡이 충분한 장르가 없습니다 "
            f"(충분한 장르: {', '.join(eligible) or '없음'}). 길이를 줄이거나 `sync` 로 풀을 늘리세요."
        )
    return tuple(rng.sample(eligible, count))


def cmd_make(args, settings: Settings) -> int:
    profiles = load_profiles(settings.profiles_file)
    random_genres = re.fullmatch(r"random(?::(\d+))?", (args.genre or "").strip().lower())
    if random_genres:
        args.genre = "all"  # pool = every genre; one is drawn below
    criteria = build_criteria(args, settings, profiles)
    if random_genres:
        count = max(1, int(random_genres.group(1) or 1))
        with _open_db(settings) as db:
            pool = filter_candidates(db, criteria)
        criteria = dataclasses.replace(
            criteria, genres=pick_random_genres(pool, args.length, count, random.Random(args.seed))
        )
        print(f"랜덤 장르 선택: {', '.join(criteria.genres)}")
    name = args.name or f"{'-'.join(criteria.genres[:2]) or 'mix'}-{args.length // 60}min"
    with _open_db(settings) as db:
        playlist, notes = pl.create(
            settings,
            db,
            name=name,
            target_seconds=args.length,
            criteria=criteria,
            exact=args.exact,
            seed=args.seed,
            tolerance=args.tolerance,
        )
        playlist.save(settings)
        _print_playlist(settings, db, playlist)
        for note in notes:
            print(f"⚠️  {note}")
        if not playlist.items:
            return 1
        if args.no_render:
            print("\n다음: `show` 로 확인 → `remove`/`add`/`fill` 로 편집 → `render`")
            return 0
        print("\n다운로드 + 합치는 중…")
        try:
            out = pl.render(settings, db, playlist, progress=print)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ {exc}")
            return 1
        print(f"\n✅ {out}\n설명란 텍스트: {playlist.folder(settings) / 'description.txt'}")
        print("마음에 들면 `commit`, 바꾸고 싶으면 `remove N` / `add 곡` 후 `render`.")
    return 0


def _load(args, settings: Settings) -> Playlist:
    try:
        return Playlist.load(settings, args.playlist)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from None


def cmd_show(args, settings: Settings) -> int:
    p = _load(args, settings)
    with _open_db(settings) as db:
        _print_playlist(settings, db, p)
    return 0


def cmd_remove(args, settings: Settings) -> int:
    p = _load(args, settings)
    with _open_db(settings) as db:
        try:
            removed = p.remove(args.numbers)
        except IndexError as exc:
            raise SystemExit(str(exc)) from None
        for item in removed:
            print(f"제거: {item.artist} - {item.title}")
            if args.ban:
                db.set_status(item.track_id, "banned", "user removed")
        if not args.no_fill:
            for note in p.fill(settings, db, seed=args.seed):
                print(f"⚠️  {note}")
        p.save(settings)
        _print_playlist(settings, db, p)
    print("\n변경 반영: `render`")
    return 0


def cmd_add(args, settings: Settings) -> int:
    p = _load(args, settings)
    with _open_db(settings) as db:
        hits = db.find(args.track, limit=8)
        if len(hits) != 1:
            if not hits:
                raise SystemExit(f"곡을 찾을 수 없습니다: {args.track}")
            print("여러 곡이 일치합니다. 아래 id 중 하나를 지정하세요:")
            for t in hits:
                print(f"  {t.id}  {t.label} ({fmt_time(t.duration)})")
            return 1
        try:
            p.add(hits[0], args.at)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        p.save(settings)
        _print_playlist(settings, db, p)
    print("\n길이가 목표와 다르면 `remove`(자동 채움) 또는 `fill` 을 쓰세요. 반영: `render`")
    return 0


def cmd_fill(args, settings: Settings) -> int:
    p = _load(args, settings)
    with _open_db(settings) as db:
        for note in p.fill(settings, db, seed=args.seed):
            print(f"⚠️  {note}")
        p.save(settings)
        _print_playlist(settings, db, p)
    return 0


def cmd_render(args, settings: Settings) -> int:
    p = _load(args, settings)
    with _open_db(settings) as db:
        try:
            out = pl.render(settings, db, p, progress=print)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ {exc}")
            return 1
    print(f"✅ {out}")
    return 0


def cmd_commit(args, settings: Settings) -> int:
    p = _load(args, settings)
    with _open_db(settings) as db:
        pl.commit(db, p, settings)
    print(f"사용 처리 완료 ({len(p.items)}곡): 다음 플레이리스트에서는 제외됩니다.")
    return 0


def cmd_ban(args, settings: Settings) -> int:
    with _open_db(settings) as db:
        for ref in args.tracks:
            hits = db.find(ref, limit=2)
            if len(hits) != 1:
                print(f"건너뜀(정확히 1곡이어야 함): {ref}")
                continue
            db.set_status(hits[0].id, "banned", "user banned")
            print(f"제외 처리: {hits[0].label}")
    return 0


def cmd_search(args, settings: Settings) -> int:
    with _open_db(settings) as db:
        hits = db.find(args.text, limit=args.limit)
    for t in hits:
        print(
            f"{t.id}  {t.label} ({fmt_time(t.duration)})  {t.genres} {t.moods} "
            f"{t.vocals} {t.source}/{t.license}"
        )
    if not hits:
        print("결과 없음")
    return 0


def cmd_migrate(args, settings: Settings) -> int:
    with _open_db(settings) as db:
        try:
            report = legacy.migrate(settings, db, Path(args.path), copy_audio=not args.no_audio)
        except FileNotFoundError as exc:
            raise SystemExit(str(exc)) from None
    print("\n".join(report.lines()))
    return 0


# ------------------------------------------------------------------------------ parser
def _add_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", default="relax", help="검색/필터 프로필 (기본 relax)")
    p.add_argument(
        "--genre",
        help=f"쉼표 구분. 예: jazz,newage,classic (all=제한 없음). 가능: {', '.join(GENRES[:9])}…",
    )
    p.add_argument(
        "--mood", help=f"쉼표 구분. 예: calm,peaceful (all=제한 없음). 가능: {', '.join(MOODS)}"
    )
    p.add_argument(
        "--vocals",
        choices=["strict", "lenient", "any"],
        help="strict=연주곡만, lenient=보컬 확정곡만 제외(기본), any=전부",
    )
    p.add_argument("--source", help="쉼표 구분: jamendo,openverse,youtube,local")
    p.add_argument("--strict-mood", action="store_true", help="mood 라벨이 없는 곡도 제외")
    p.add_argument("--allow-reuse", action="store_true", help="이미 사용(commit)한 곡도 허용")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-crawler", description="잔잔한 연주곡을 모아 원하는 길이의 배경음악을 만듭니다."
    )
    parser.add_argument("--home", help="작업 폴더 (기본: 현재 폴더 또는 MUSIC_CRAWLER_HOME)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="작업 폴더/.env 준비").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="환경 점검").set_defaults(func=cmd_doctor)

    p = sub.add_parser("stats", help="후보 풀 현황 (조건별 곡 수/시간)")
    _add_filters(p)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("sync", help="출처에서 메타데이터 수집")
    p.add_argument(
        "source",
        nargs="?",
        default="all",
        choices=["all", "jamendo", "openverse", "local", "youtube"],
    )
    p.add_argument("--profile", default="relax")
    p.add_argument(
        "--per-tag",
        type=int,
        default=400,
        help="Jamendo: 이번 실행에서 태그당 '추가로' 확인할 곡 수 (이어서 진행, 기본 400)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Jamendo: 모든 태그를 끝까지 수집 (중단해도 다음에 이어서 진행)",
    )
    p.add_argument("--reset", action="store_true", help="Jamendo: 진행 위치를 지우고 처음부터")
    p.add_argument("--per-query", type=int, default=240, help="Openverse 검색어당 최대 곡 수")
    p.add_argument("--path", help="local: 스캔할 폴더 (기본 library/inbox)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("make", help="목표 길이 플레이리스트 생성(+다운로드+합치기)")
    p.add_argument("length", type=parse_length, help="목표 길이. 60=60분, 45:00=45분, 1:15:00")
    p.add_argument("--name", help="플레이리스트/영상 이름")
    _add_filters(p)
    p.add_argument(
        "--exact",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="마지막 곡을 페이드아웃으로 잘라 길이를 정확히 맞춤 (기본 켜짐, 끄려면 --no-exact)",
    )
    p.add_argument(
        "--tolerance", type=int, default=120, help="목표보다 길어도 되는 허용 초 (기본 120)"
    )
    p.add_argument("--seed", type=int, help="같은 결과를 재현하려면 지정")
    p.add_argument("--no-render", action="store_true", help="곡만 고르고 다운로드/합치기는 나중에")
    p.set_defaults(func=cmd_make)

    def playlist_cmd(name: str, help_: str, func):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument(
            "-p", "--playlist", default="latest", help="플레이리스트 id/일부 (기본 latest)"
        )
        sp.set_defaults(func=func)
        return sp

    playlist_cmd("show", "플레이리스트 보기", cmd_show)
    sp = playlist_cmd("remove", "곡 삭제(+자동으로 길이 다시 채움)", cmd_remove)
    sp.add_argument("numbers", nargs="+", type=int, help="삭제할 번호(1부터)")
    sp.add_argument("--ban", action="store_true", help="삭제한 곡을 앞으로도 추천하지 않음")
    sp.add_argument("--no-fill", action="store_true", help="삭제 후 자동 채움 안 함")
    sp.add_argument("--seed", type=int)
    sp = playlist_cmd("add", "곡 추가 (id 또는 검색어)", cmd_add)
    sp.add_argument("track")
    sp.add_argument("--at", type=int, help="넣을 위치(1부터), 기본 맨 뒤")
    sp = playlist_cmd("fill", "목표 길이에 맞게 다시 채움", cmd_fill)
    sp.add_argument("--seed", type=int)
    playlist_cmd("render", "다운로드 + 하나의 mp3로 합치기", cmd_render)
    playlist_cmd("commit", "사용 처리(이후 추천에서 제외)", cmd_commit)

    sp = sub.add_parser("ban", help="곡을 영구 제외")
    sp.add_argument("tracks", nargs="+")
    sp.set_defaults(func=cmd_ban)
    sp = sub.add_parser("search", help="곡 검색")
    sp.add_argument("text")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_search)
    sp = sub.add_parser("migrate-legacy", help="이전 프로젝트(tracks.db 등) 가져오기")
    sp.add_argument("path", help="이전 프로젝트 폴더")
    sp.add_argument("--no-audio", action="store_true", help="오디오 파일은 복사하지 않음")
    sp.set_defaults(func=cmd_migrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows cp949 consoles
        except (AttributeError, OSError):
            pass
    args = build_parser().parse_args(argv)
    settings = Settings.load(args.home)
    try:
        return int(args.func(args, settings) or 0)
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        return 130
