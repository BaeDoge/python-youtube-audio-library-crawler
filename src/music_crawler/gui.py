"""Small Tkinter desktop menu for the music library workflow.

This module is imported only when the user launches the menu. The CLI remains available.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from pathlib import Path
from tkinter import END, MULTIPLE, BooleanVar, StringVar, Tk, Toplevel, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from music_crawler import audio
from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.playlist import Playlist, fmt_time
from music_crawler.providers import local
from music_crawler.selector import Criteria
from music_crawler.taxonomy import GENRES, MOODS, load_profiles


def _open_path(path: Path) -> None:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"경로가 없습니다: {path}")
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif os.sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _sync_online(name: str, settings: Settings, db: Database, profile, log) -> None:
    log(f"[{name}] 수집 시작")
    if name == "jamendo":
        if not settings.jamendo_client_id:
            log("JAMENDO_CLIENT_ID가 없어 건너뜁니다. .env에 키를 입력하세요.")
            return
        from music_crawler.providers import jamendo
        report = jamendo.sync(settings, db, profile, per_tag_limit=400, progress=log)
    elif name == "openverse":
        from music_crawler.providers import openverse
        report = openverse.sync(settings, db, profile, per_query_limit=240, progress=log)
    elif name == "youtube":
        from music_crawler.providers import youtube_library
        report = youtube_library.sync(settings, db, profile, progress=log)
    else:
        raise ValueError(f"지원하지 않는 플랫폼: {name}")
    for line in report.lines():
        log(line)


class MusicLibraryApp:
    """Five-entry desktop menu; long-running work is moved to a worker thread."""

    def __init__(self, settings: Settings):
        import tkinter as tk

        self.tk = tk
        self.settings = settings
        self.settings.ensure_dirs()
        self.root = Tk()
        self.root.title("Music Library")
        self.root.geometry("1080x760")
        self.root.minsize(940, 660)
        self.root.configure(background="#f4f6f8")
        self.busy = False
        self.task_queue: queue.Queue = queue.Queue()
        self.task_window = None
        self.task_log = None
        self.task_progress = None
        self.latest_playlist: Playlist | None = None

        self._style()
        self._build_shell()
        self.show_home()
        self.refresh_summary()

    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except self.tk.TclError:
            pass
        style.configure("App.TFrame", background="#f4f6f8")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Title.TLabel", background="#f4f6f8", foreground="#17212b",
                        font=("맑은 고딕", 22, "bold"))
        style.configure("Subtitle.TLabel", background="#f4f6f8", foreground="#65717d",
                        font=("맑은 고딕", 9))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#17212b",
                        font=("맑은 고딕", 12, "bold"))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#65717d",
                        font=("맑은 고딕", 9))
        style.configure("Accent.TButton", font=("맑은 고딕", 10, "bold"), padding=(16, 10))
        style.configure("Treeview", rowheight=29, font=("맑은 고딕", 9))
        style.configure("Treeview.Heading", font=("맑은 고딕", 9, "bold"))
        style.configure("TLabel", font=("맑은 고딕", 9))
        style.configure("TButton", font=("맑은 고딕", 9), padding=(9, 6))
        style.configure("TCheckbutton", background="#ffffff", font=("맑은 고딕", 9))
        style.configure("TRadiobutton", background="#ffffff", font=("맑은 고딕", 9))

    def _build_shell(self) -> None:
        shell = ttk.Frame(self.root, style="App.TFrame", padding=(28, 22))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell, style="App.TFrame")
        header.pack(fill="x", pady=(0, 18))
        brand = ttk.Frame(header, style="App.TFrame")
        brand.pack(side="left", fill="x", expand=True)
        ttk.Label(brand, text="MUSIC LIBRARY", style="Title.TLabel").pack(anchor="w")
        ttk.Label(brand, text="음원 수집부터 믹스 생성과 라이브러리 관리까지",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Button(header, text="메인 메뉴", command=self.show_home).pack(side="right", padx=(8, 0))
        ttk.Button(header, text="작업 폴더 열기",
                   command=lambda: self.open_path(self.settings.home)).pack(side="right")
        self.summary = ttk.Label(shell, text="", style="Subtitle.TLabel")
        self.summary.pack(fill="x", pady=(0, 14))
        self.content = ttk.Frame(shell, style="App.TFrame")
        self.content.pack(fill="both", expand=True)
        footer = ttk.Frame(shell, style="App.TFrame")
        footer.pack(fill="x", pady=(14, 0))
        self.status = StringVar(value="준비")
        ttk.Label(footer, textvariable=self.status, style="Subtitle.TLabel").pack(side="left")
        ttk.Label(footer, text=str(self.settings.home), style="Subtitle.TLabel").pack(side="right")

    def _clear(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()

    def _page_title(self, title: str, description: str) -> None:
        head = ttk.Frame(self.content, style="App.TFrame")
        head.pack(fill="x", pady=(0, 16))
        ttk.Label(head, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, text=description, style="Subtitle.TLabel").pack(anchor="w", pady=(4, 0))

    def _card(self, parent, padding=18):
        return ttk.Frame(parent, style="Card.TFrame", padding=padding)

    def _section_label(self, parent, text: str) -> None:
        ttk.Label(parent, text=text, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 10))

    def refresh_summary(self) -> None:
        try:
            with Database(self.settings.db_path) as db:
                counts = db.count_by_source()
                total = sum(counts.values())
                used = len(db.used_track_ids())
                banned = len(db.all_tracks(status="banned"))
            self.summary.configure(text=f"등록된 후보 {total:,}곡     USED {used:,}곡     BAN {banned:,}곡")
        except Exception as exc:  # noqa: BLE001
            self.summary.configure(text=f"라이브러리 현황을 읽지 못했습니다: {exc}")

    # ------------------------------------------------------------------ home
    def show_home(self) -> None:
        self._clear()
        self._page_title("무엇을 할까요?", "자주 사용하는 기능만 모았습니다. 원하는 작업을 선택하세요.")
        grid = ttk.Frame(self.content, style="App.TFrame")
        grid.pack(fill="both", expand=True)
        actions = [
            ("01", "믹스 만들기", "목표 길이와 장르·무드를 고르고 통합 음원을 생성합니다.", self.show_make),
            ("02", "음원 라이브러리", "직접 받은 음원을 등록하거나 외부 플랫폼에서 수집합니다.", self.show_library),
            ("03", "믹스 검토 및 수정", "최신 믹스를 듣고 마음에 들지 않는 곡을 교체합니다.", self.show_edit),
            ("04", "사용 / 제외 목록", "USED와 BAN 목록을 확인하고 제외한 곡을 복구합니다.", self.show_lists),
            ("05", "환경 및 폴더", "FFmpeg 상태, 플랫폼 인증, 음원·결과 폴더를 확인합니다.", self.show_settings),
        ]
        for index, (number, title, description, command) in enumerate(actions):
            row, col = divmod(index, 2)
            card = self._card(grid, padding=18)
            card.grid(row=row, column=col, sticky="nsew",
                      padx=(0, 10) if col == 0 else (10, 0), pady=8)
            ttk.Label(card, text=number, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(5, 4))
            ttk.Label(card, text=description, style="Muted.TLabel",
                      wraplength=390).pack(anchor="w", fill="x", pady=(0, 14))
            ttk.Button(card, text="열기  →", style="Accent.TButton",
                       command=command).pack(anchor="e")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        for row in range(3):
            grid.rowconfigure(row, weight=1)
        note = self._card(self.content, padding=14)
        note.pack(fill="x", pady=(10, 0))
        ttk.Label(
            note,
            text="빠른 안내  ·  직접 받은 음원은 library/inbox/장르/무드/ 에 넣으세요. "
                 "선택된 곡의 원본 파일이 없다면 믹스 렌더링 단계에서 자동 다운로드됩니다.",
            style="Muted.TLabel", wraplength=900,
        ).pack(anchor="w")

    # ------------------------------------------------------------------ mix creation
    def show_make(self) -> None:
        self._clear()
        self._page_title("믹스 만들기", "길이와 무드를 정하면 선곡·다운로드·합성을 자동으로 진행합니다.")
        card = self._card(self.content, padding=20)
        card.pack(fill="both", expand=True)

        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="목표 길이 (분)", style="CardTitle.TLabel").pack(side="left")
        self.duration_var = StringVar(value="60")
        ttk.Entry(row, textvariable=self.duration_var, width=9).pack(side="left", padx=(12, 6))
        ttk.Label(row, text="예: 60", style="Muted.TLabel").pack(side="left")
        ttk.Label(row, text="믹스 이름", style="CardTitle.TLabel").pack(side="left", padx=(28, 8))
        self.mix_name_var = StringVar()
        ttk.Entry(row, textvariable=self.mix_name_var, width=24).pack(side="left")

        selectors = ttk.Frame(card, style="Card.TFrame")
        selectors.pack(fill="both", expand=True, pady=(20, 10))
        genre_box = ttk.Frame(selectors, style="Card.TFrame")
        genre_box.pack(side="left", fill="both", expand=True, padx=(0, 10))
        mood_box = ttk.Frame(selectors, style="Card.TFrame")
        mood_box.pack(side="left", fill="both", expand=True, padx=(10, 0))
        ttk.Label(genre_box, text="장르 선택 (복수 선택 가능)",
                  style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(genre_box, text="선택하지 않으면 전체 장르",
                  style="Muted.TLabel").pack(anchor="w", pady=(3, 7))
        ttk.Label(mood_box, text="무드 선택 (복수 선택 가능)",
                  style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(mood_box, text="선택하지 않으면 전체 무드",
                  style="Muted.TLabel").pack(anchor="w", pady=(3, 7))
        self.genre_list = self._make_listbox(genre_box, GENRES, height=9)
        self.mood_list = self._make_listbox(mood_box, MOODS, height=9)
        defaults = {"Jazz", "NewAge", "Classical", "Ambient", "Piano", "Acoustic", "Lounge"}
        for index, genre in enumerate(GENRES):
            if genre in defaults:
                self.genre_list.selection_set(index)
        for index, mood in enumerate(MOODS):
            if mood in {"calm", "peaceful", "warm", "dreamy", "romantic", "melancholy"}:
                self.mood_list.selection_set(index)

        source_card = ttk.Frame(card, style="Card.TFrame")
        source_card.pack(fill="x", pady=(10, 0))
        ttk.Label(source_card, text="음원 가져오기", style="CardTitle.TLabel").pack(anchor="w")
        self.source_mode = StringVar(value="local")
        options = ttk.Frame(source_card, style="Card.TFrame")
        options.pack(fill="x", pady=(7, 5))
        ttk.Radiobutton(options, text="보유 음원만 사용", variable=self.source_mode,
                        value="local", command=self._toggle_source_options).pack(side="left", padx=(0, 24))
        ttk.Radiobutton(options, text="온라인 플랫폼도 선택", variable=self.source_mode,
                        value="online", command=self._toggle_source_options).pack(side="left")
        self.source_options = ttk.Frame(source_card, style="Card.TFrame")
        self.source_options.pack(fill="x", padx=(24, 0))
        self.provider_vars = {
            "jamendo": BooleanVar(value=True),
            "openverse": BooleanVar(value=True),
            "youtube": BooleanVar(value=False),
        }
        for name, label in (("jamendo", "Jamendo"), ("openverse", "Openverse"),
                            ("youtube", "YouTube Audio Library")):
            ttk.Checkbutton(self.source_options, text=label,
                            variable=self.provider_vars[name]).pack(side="left", padx=(0, 18))
        self._toggle_source_options()
        self.exact_var = BooleanVar(value=True)
        ttk.Checkbutton(source_card, text="마지막 곡을 잘라 목표 길이에 정확히 맞추기",
                        variable=self.exact_var).pack(anchor="w", pady=(9, 0))

        footer = ttk.Frame(card, style="Card.TFrame")
        footer.pack(fill="x", pady=(18, 0))
        ttk.Button(footer, text="취소", command=self.show_home).pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="믹스 생성 시작", style="Accent.TButton",
                   command=self.create_mix).pack(side="right")

    def _make_listbox(self, parent, values, height=8):
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="both", expand=True, pady=(0, 4))
        listbox = self.tk.Listbox(
            frame, selectmode=MULTIPLE, height=height, exportselection=False,
            font=("맑은 고딕", 9), relief="flat", borderwidth=0,
            highlightthickness=1, highlightbackground="#d7dde3",
            selectbackground="#dcecff", selectforeground="#17212b",
        )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for value in values:
            listbox.insert(END, value)
        return listbox

    def _toggle_source_options(self) -> None:
        state = "normal" if self.source_mode.get() == "online" else "disabled"
        for child in self.source_options.winfo_children():
            child.configure(state=state)

    def create_mix(self) -> None:
        try:
            minutes = int(self.duration_var.get().strip())
            if minutes < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("길이 확인", "목표 길이는 1 이상의 정수(분)로 입력하세요.",
                                 parent=self.root)
            return
        genres = tuple(GENRES[i] for i in self.genre_list.curselection())
        moods = tuple(MOODS[i] for i in self.mood_list.curselection())
        online = [name for name, var in self.provider_vars.items() if var.get()] \
            if self.source_mode.get() == "online" else []
        if self.source_mode.get() == "online" and not online:
            messagebox.showwarning("플랫폼 선택", "자동 수집에 사용할 플랫폼을 하나 이상 선택하세요.",
                                   parent=self.root)
            return
        sources = tuple(["local", *online])
        name = self.mix_name_var.get().strip() or f"{'-'.join(genres[:2]) or 'mix'}-{minutes}min"
        exact = self.exact_var.get()

        def worker(log):
            self.settings.ensure_dirs()
            profile = load_profiles(self.settings.profiles_file)["relax"]
            criteria = Criteria(
                genres=genres, moods=moods, vocals="lenient", sources=sources,
                exclude_used=True, min_seconds=self.settings.min_track_seconds,
                max_seconds=self.settings.max_track_seconds,
            )
            with Database(self.settings.db_path) as db:
                log("내 음원 폴더를 확인하고 있습니다.")
                report = local.sync(self.settings, db)
                for line in report.lines():
                    log(line)
                playlist, notes = self._create_playlist_with_fallback(
                    db, profile, name, minutes * 60, criteria, online, exact, log
                )
                if not playlist.items:
                    return {"playlist": playlist, "output": None, "notes": notes}
                playlist.save(self.settings)
                log(f"선곡 완료: {len(playlist.items)}곡")
                log("다운로드 및 통합 음원을 생성합니다.")
                output = self._render_playlist(db, playlist, log)
                return {"playlist": playlist, "output": output, "notes": notes}
        self._run_task("믹스 생성", worker, self._mix_created)

    def _create_playlist_with_fallback(self, db, profile, name, target, criteria,
                                       online, exact, log):
        from music_crawler import playlist as pl

        playlist, notes = pl.create(
            self.settings, db, name=name, target_seconds=target, criteria=criteria,
            exact=exact, tolerance=120,
        )
        for source in online:
            if playlist.length(self.settings.crossfade_seconds) >= target:
                break
            _sync_online(source, self.settings, db, profile, log)
            notes.extend(playlist.fill(self.settings, db, tolerance=120))
        if playlist.length(self.settings.crossfade_seconds) < target and criteria.moods:
            log("선택한 무드 음원이 부족해 같은 장르의 다른 무드 곡으로 보충합니다.")
            from dataclasses import replace
            fallback = replace(criteria, moods=())
            playlist.criteria = fallback.as_dict()
            notes.extend(playlist.fill(self.settings, db, tolerance=120))
        return playlist, notes

    def _render_playlist(self, db, playlist, log):
        from music_crawler import playlist as pl
        return pl.render(self.settings, db, playlist, progress=log)

    def _mix_created(self, result) -> None:
        playlist = result["playlist"]
        self.latest_playlist = playlist
        if result["output"] is None:
            messagebox.showwarning(
                "음원 부족",
                "조건에 맞는 곡을 찾지 못했습니다.\n장르·무드 범위를 넓히거나 라이브러리에 음원을 추가하세요.",
                parent=self.root,
            )
            self.refresh_summary()
            return
        note = "\n".join(result["notes"]) if result["notes"] else "추가 안내 없음"
        messagebox.showinfo(
            "믹스 생성 완료",
            f"파일: {result['output']}\n선곡: {len(playlist.items)}곡\n\n{note}",
            parent=self.root,
        )
        self.refresh_summary()
        self.show_edit()

    # ------------------------------------------------------------------ library
    def show_library(self) -> None:
        self._clear()
        self._page_title("음원 라이브러리", "직접 받은 음원을 등록하거나 선택한 외부 플랫폼의 카탈로그를 수집합니다.")
        card = self._card(self.content, padding=20)
        card.pack(fill="both", expand=True)
        self._section_label(card, "내 음원")
        ttk.Label(card, text="음원 파일을 library/inbox/장르/무드/ 폴더에 넣으세요.",
                  style="Muted.TLabel").pack(anchor="w")
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.pack(fill="x", pady=(12, 22))
        ttk.Button(actions, text="음원 넣을 폴더 열기",
                   command=lambda: self.open_path(self.settings.inbox_dir)).pack(side="left")
        ttk.Button(actions, text="내 음원 다시 읽기", style="Accent.TButton",
                   command=self.scan_local).pack(side="left", padx=(8, 0))
        self._section_label(card, "외부 플랫폼")
        ttk.Label(card, text="사용할 플랫폼을 선택한 뒤 동기화를 누르세요.",
                  style="Muted.TLabel").pack(anchor="w")
        self.library_provider_vars = {
            "jamendo": BooleanVar(value=True),
            "openverse": BooleanVar(value=True),
            "youtube": BooleanVar(value=False),
        }
        providers = ttk.Frame(card, style="Card.TFrame")
        providers.pack(fill="x", pady=(10, 14))
        for name, label in (("jamendo", "Jamendo"), ("openverse", "Openverse"),
                            ("youtube", "YouTube Audio Library")):
            ttk.Checkbutton(providers, text=label,
                            variable=self.library_provider_vars[name]).pack(side="left", padx=(0, 20))
        ttk.Button(card, text="선택한 플랫폼 동기화", style="Accent.TButton",
                   command=self.sync_library).pack(anchor="e")
        note = self._card(self.content, padding=14)
        note.pack(fill="x", pady=(14, 0))
        ttk.Label(note, text="Jamendo는 API Client ID가 필요합니다. YouTube Audio Library는 로그인된 Chrome과 Playwright가 필요합니다.",
                  style="Muted.TLabel", wraplength=850).pack(anchor="w")

    def scan_local(self) -> None:
        def worker(log):
            self.settings.ensure_dirs()
            with Database(self.settings.db_path) as db:
                report = local.sync(self.settings, db)
            for line in report.lines():
                log(line)
            return report
        self._run_task("내 음원 등록", worker, lambda _result: self._library_done())

    def sync_library(self) -> None:
        selected = [name for name, var in self.library_provider_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("플랫폼 선택", "동기화할 플랫폼을 선택하세요.", parent=self.root)
            return
        def worker(log):
            profile = load_profiles(self.settings.profiles_file)["relax"]
            with Database(self.settings.db_path) as db:
                local_report = local.sync(self.settings, db)
                for line in local_report.lines():
                    log(line)
                for name in selected:
                    _sync_online(name, self.settings, db, profile, log)
        self._run_task("플랫폼 동기화", worker, lambda _result: self._library_done())

    def _library_done(self) -> None:
        self.refresh_summary()
        messagebox.showinfo("완료", "라이브러리 업데이트가 끝났습니다.", parent=self.root)

    # ------------------------------------------------------------------ edit mix
    def show_edit(self) -> None:
        self._clear()
        self._page_title("믹스 검토 및 수정", "최신 믹스의 곡을 듣고, 마음에 들지 않는 곡은 자동으로 교체합니다.")
        top = ttk.Frame(self.content, style="App.TFrame")
        top.pack(fill="x", pady=(0, 12))
        ttk.Button(top, text="최신 믹스 다시 불러오기", command=self.refresh_edit).pack(side="left")
        ttk.Button(top, text="통합 음원 재생", command=self.play_mix).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="폴더 열기", command=self.open_latest_folder).pack(side="left", padx=(8, 0))

        card = self._card(self.content, padding=16)
        card.pack(fill="both", expand=True)
        columns = ("number", "start", "duration", "title", "source", "license")
        self.edit_tree = ttk.Treeview(card, columns=columns, show="headings", selectmode="extended")
        headers = {"number": "#", "start": "시작", "duration": "길이", "title": "곡",
                   "source": "출처", "license": "라이선스"}
        widths = {"number": 44, "start": 85, "duration": 85, "title": 420, "source": 100,
                  "license": 130}
        for key in columns:
            self.edit_tree.heading(key, text=headers[key])
            self.edit_tree.column(key, width=widths[key],
                                  anchor="w" if key == "title" else "center")
        self.edit_tree.pack(fill="both", expand=True)
        self.edit_tree.bind("<Double-1>", lambda _event: self.play_selected_track())
        toolbar = ttk.Frame(card, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(12, 0))
        ttk.Button(toolbar, text="선택 곡 듣기", command=self.play_selected_track).pack(side="left")
        self.ban_removed_var = BooleanVar(value=True)
        ttk.Checkbutton(toolbar, text="제거한 곡을 전역 BAN에 등록",
                        variable=self.ban_removed_var).pack(side="left", padx=(14, 0))
        ttk.Button(toolbar, text="선택 곡 제외 + 자동 채움 + 재생성",
                   style="Accent.TButton", command=self.remove_and_refill).pack(side="right")
        add_row = ttk.Frame(self.content, style="App.TFrame")
        add_row.pack(fill="x", pady=(12, 0))
        ttk.Label(add_row, text="라이브러리에서 곡 추가").pack(side="left")
        self.add_track_var = StringVar()
        ttk.Entry(add_row, textvariable=self.add_track_var, width=36).pack(side="left", padx=8)
        ttk.Button(add_row, text="검색한 곡 추가 + 재생성", command=self.add_track).pack(side="left")
        self.refresh_edit()

    def refresh_edit(self) -> None:
        if not hasattr(self, "edit_tree") or not self.edit_tree.winfo_exists():
            return
        for row in self.edit_tree.get_children():
            self.edit_tree.delete(row)
        try:
            playlist = Playlist.load(self.settings, "latest")
        except FileNotFoundError:
            self.latest_playlist = None
            self.status.set("아직 생성된 믹스가 없습니다.")
            return
        self.latest_playlist = playlist
        crossfade = self.settings.crossfade_seconds
        trim = playlist.trim_for_exact(crossfade)
        for number, (start, item) in enumerate(playlist.timeline(crossfade), 1):
            duration = trim if number == len(playlist.items) and trim else item.duration
            title = f"{item.artist} - {item.title}" if item.artist else item.title
            self.edit_tree.insert("", END, values=(number, fmt_time(start), fmt_time(duration),
                                                    title, item.source, item.license))
        self.status.set(f"{playlist.name}  |  {len(playlist.items)}곡  |  "
                        f"목표 {fmt_time(playlist.target_seconds)}  |  상태 {playlist.status}")

    def play_mix(self) -> None:
        try:
            playlist = self._load_latest()
            self.open_path(playlist.folder(self.settings) / "mix.mp3")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("재생할 수 없습니다", str(exc), parent=self.root)

    def play_selected_track(self) -> None:
        selection = self.edit_tree.selection() if hasattr(self, "edit_tree") else ()
        if not selection:
            messagebox.showinfo("곡 선택", "먼저 재생할 곡을 선택하세요.", parent=self.root)
            return
        number = int(self.edit_tree.item(selection[0], "values")[0])
        try:
            playlist = self._load_latest()
            item = playlist.items[number - 1]
            with Database(self.settings.db_path) as db:
                track = db.get(item.track_id)
            if not track or not track.file_path:
                raise FileNotFoundError("이 곡은 아직 다운로드되지 않았습니다.")
            self.open_path(self.settings.from_stored(track.file_path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("곡을 열 수 없습니다", str(exc), parent=self.root)

    def remove_and_refill(self) -> None:
        selected = self.edit_tree.selection()
        if not selected:
            messagebox.showinfo("곡 선택", "제외할 곡을 목록에서 선택하세요.", parent=self.root)
            return
        numbers = sorted({int(self.edit_tree.item(row, "values")[0]) for row in selected})
        ban = self.ban_removed_var.get()
        prompt = f"{len(numbers)}곡을 제외하고 빈자리를 자동으로 채운 뒤 다시 렌더링합니다."
        if ban:
            prompt += "\n제외한 곡은 다음 믹스에서도 추천하지 않도록 BAN 목록에 등록합니다."
        if not messagebox.askyesno("믹스 수정", prompt, parent=self.root):
            return
        def worker(log):
            from music_crawler import playlist as pl
            playlist = self._load_latest()
            with Database(self.settings.db_path) as db:
                removed = playlist.remove(numbers)
                for item in removed:
                    log(f"제외: {item.artist} - {item.title}")
                    if ban:
                        db.set_status(item.track_id, "banned", "removed from desktop mix editor")
                for note in playlist.fill(self.settings, db):
                    log(note)
                playlist.save(self.settings)
                output = pl.render(self.settings, db, playlist, progress=log)
            return {"playlist": playlist, "output": output}
        self._run_task("믹스 수정 및 재생성", worker, self._edit_done)

    def add_track(self) -> None:
        query = self.add_track_var.get().strip()
        if not query:
            return
        def worker(log):
            from music_crawler import playlist as pl
            playlist = self._load_latest()
            with Database(self.settings.db_path) as db:
                hits = db.find(query, limit=8)
                if len(hits) != 1:
                    if not hits:
                        raise ValueError(f"곡을 찾을 수 없습니다: {query}")
                    choices = "\n".join(f"{track.id}  {track.label}" for track in hits)
                    raise ValueError("여러 곡이 일치합니다. 검색어를 더 구체적으로 입력하세요.\n" + choices)
                track = hits[0]
                if track.status != "candidate":
                    raise ValueError(f"현재 추가할 수 없는 곡입니다. 상태: {track.status}")
                playlist.add(track)
                playlist.save(self.settings)
                output = pl.render(self.settings, db, playlist, progress=log)
            return {"playlist": playlist, "output": output}
        self._run_task("곡 추가 및 재생성", worker, self._edit_done)

    def _load_latest(self) -> Playlist:
        return Playlist.load(self.settings, "latest")

    def _edit_done(self, result) -> None:
        self.latest_playlist = result["playlist"]
        self.refresh_edit()
        self.refresh_summary()
        messagebox.showinfo("믹스 갱신 완료", f"새 파일을 만들었습니다.\n{result['output']}",
                            parent=self.root)

    def open_latest_folder(self) -> None:
        try:
            playlist = self._load_latest()
            self.open_path(playlist.folder(self.settings))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("폴더를 열 수 없습니다", str(exc), parent=self.root)

    # ------------------------------------------------------------------ USED / BAN
    def show_lists(self) -> None:
        self._clear()
        self._page_title("사용 / 제외 목록", "USED는 이미 최종 확정한 곡, BAN은 직접 제외한 곡입니다.")
        tabs = ttk.Notebook(self.content)
        tabs.pack(fill="both", expand=True)
        ban_frame = ttk.Frame(tabs, padding=14)
        used_frame = ttk.Frame(tabs, padding=14)
        tabs.add(ban_frame, text="BAN 목록")
        tabs.add(used_frame, text="USED 목록")
        self.ban_tree = self._make_history_tree(ban_frame)
        self.used_tree = self._make_history_tree(used_frame)
        toolbar = ttk.Frame(ban_frame)
        toolbar.pack(fill="x", pady=(10, 0))
        ttk.Button(toolbar, text="선택한 곡 BAN 해제", style="Accent.TButton",
                   command=self.unban_selected).pack(side="right")
        self.refresh_lists()

    def _make_history_tree(self, parent):
        columns = ("id", "title", "artist", "source", "reason")
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
        labels = {"id": "ID", "title": "곡", "artist": "아티스트", "source": "출처",
                  "reason": "사유"}
        widths = {"id": 150, "title": 280, "artist": 180, "source": 100, "reason": 240}
        for col in columns:
            tree.heading(col, text=labels[col])
            tree.column(col, width=widths[col], anchor="w")
        tree.pack(fill="both", expand=True)
        return tree

    def refresh_lists(self) -> None:
        if not hasattr(self, "ban_tree") or not self.ban_tree.winfo_exists():
            return
        for tree in (self.ban_tree, self.used_tree):
            for row in tree.get_children():
                tree.delete(row)
        with Database(self.settings.db_path) as db:
            banned = db.all_tracks(status="banned")
            used = [db.get(track_id) for track_id in sorted(db.used_track_ids())]
        for track in banned:
            self.ban_tree.insert("", END, values=(track.id, track.title, track.artist,
                                                   track.source, track.status_reason))
        for track in used:
            if track:
                self.used_tree.insert("", END, values=(track.id, track.title, track.artist,
                                                        track.source, "확정 사용"))

    def unban_selected(self) -> None:
        selected = self.ban_tree.selection()
        if not selected:
            messagebox.showinfo("곡 선택", "BAN 해제할 곡을 선택하세요.", parent=self.root)
            return
        track_id = self.ban_tree.item(selected[0], "values")[0]
        with Database(self.settings.db_path) as db:
            track = db.get(track_id)
            if not track or track.status != "banned":
                messagebox.showwarning("상태 변경", "현재 BAN 상태가 아닙니다.", parent=self.root)
                self.refresh_lists()
                return
            db.set_status(track_id, "candidate", "")
        self.refresh_lists()
        self.refresh_summary()
        messagebox.showinfo("완료", "BAN 목록에서 복구했습니다.", parent=self.root)

    # ------------------------------------------------------------------ settings
    def show_settings(self) -> None:
        self._clear()
        self._page_title("환경 및 폴더", "필수 실행 환경과 외부 플랫폼 연결 상태를 확인합니다.")
        card = self._card(self.content, padding=20)
        card.pack(fill="both", expand=True)
        self._section_label(card, "실행 환경")
        lines = [
            ("FFmpeg", audio.which(self.settings.ffmpeg_bin) or "찾을 수 없음"),
            ("FFprobe", audio.which(self.settings.ffprobe_bin) or "찾을 수 없음"),
            ("Jamendo Client ID", "설정됨" if self.settings.jamendo_client_id else "미설정"),
            ("Openverse Client ID", "설정됨" if self.settings.openverse_client_id else "미설정 (익명 사용 가능)"),
            ("YouTube Channel ID", "설정됨" if self.settings.youtube_channel_id else "미설정"),
        ]
        for label, value in lines:
            row = ttk.Frame(card, style="Card.TFrame")
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=label, style="CardTitle.TLabel", width=24).pack(side="left")
            ttk.Label(row, text=value, style="Muted.TLabel").pack(side="left")
        ttk.Separator(card).pack(fill="x", pady=16)
        self._section_label(card, "핵심 폴더")
        folders = [
            ("직접 받은 음원", self.settings.inbox_dir),
            ("다운로드 라이브러리", self.settings.library_dir),
            ("통합 음원 결과", self.settings.playlists_dir),
            ("환경 설정 파일", self.settings.home / ".env"),
        ]
        for label, path in folders:
            row = ttk.Frame(card, style="Card.TFrame")
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, width=22).pack(side="left")
            ttk.Label(row, text=str(path), style="Muted.TLabel").pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="열기", command=lambda p=path: self.open_path(p)).pack(side="right")
        ttk.Separator(card).pack(fill="x", pady=16)
        ttk.Label(card, text="플랫폼 인증 정보를 수정한 뒤에는 프로그램을 다시 시작하세요.",
                  style="Muted.TLabel").pack(anchor="w")
        ttk.Button(card, text=".env 열기", command=self.open_env).pack(anchor="e", pady=(8, 0))

    def open_env(self) -> None:
        env = self.settings.home / ".env"
        if not env.exists():
            env.write_text(
                "JAMENDO_CLIENT_ID=\nOPENVERSE_CLIENT_ID=\nOPENVERSE_CLIENT_SECRET=\n"
                "YOUTUBE_CHANNEL_ID=\nCDP_URL=http://127.0.0.1:9222\n"
                "FFMPEG_BIN=ffmpeg\nFFPROBE_BIN=ffprobe\n",
                encoding="utf-8",
            )
        self.open_path(env)

    # ------------------------------------------------------------------ background jobs
    def _run_task(self, title: str, worker, on_done=None) -> None:
        if self.busy:
            messagebox.showinfo("작업 중", "현재 작업이 끝난 뒤 다시 실행하세요.", parent=self.root)
            return
        self.busy = True
        self.status.set(title + " 진행 중…")
        dialog = Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("680x390")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="CardTitle.TLabel").pack(anchor="w")
        self.task_progress = ttk.Progressbar(body, mode="indeterminate")
        self.task_progress.pack(fill="x", pady=(10, 8))
        self.task_progress.start(12)
        self.task_log = ScrolledText(body, wrap="word", height=16, font=("맑은 고딕", 9))
        self.task_log.pack(fill="both", expand=True)
        self.task_window = dialog

        def log(message: str) -> None:
            self.task_queue.put(("log", str(message)))

        def run() -> None:
            try:
                result = worker(log)
                self.task_queue.put(("done", result, None, on_done))
            except Exception as exc:  # noqa: BLE001
                self.task_queue.put(("done", None, exc, on_done))
        threading.Thread(target=run, name="music-library-task", daemon=True).start()
        self.root.after(100, self._poll_task)

    def _poll_task(self) -> None:
        finished = False
        while True:
            try:
                item = self.task_queue.get_nowait()
            except queue.Empty:
                break
            if item[0] == "log":
                if self.task_log and self.task_log.winfo_exists():
                    self.task_log.insert(END, item[1] + "\n")
                    self.task_log.see(END)
            else:
                _, result, error, callback = item
                finished = True
                self.busy = False
                if self.task_progress:
                    self.task_progress.stop()
                if self.task_window and self.task_window.winfo_exists():
                    self.task_window.grab_release()
                    self.task_window.destroy()
                self.task_window = self.task_log = self.task_progress = None
                self.status.set("준비")
                self.refresh_summary()
                if error:
                    messagebox.showerror("작업 실패", str(error), parent=self.root)
                elif callback:
                    callback(result)
        if not finished and self.busy:
            self.root.after(120, self._poll_task)

    def open_path(self, path: Path) -> None:
        try:
            path = Path(path)
            if not path.exists() and path.suffix == "":
                path.mkdir(parents=True, exist_ok=True)
            _open_path(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("열 수 없습니다", str(exc), parent=self.root)

    def run(self) -> None:
        self.root.mainloop()


def launch(settings: Settings) -> None:
    try:
        MusicLibraryApp(settings).run()
    except Exception as exc:  # noqa: BLE001
        try:
            messagebox.showerror("Music Library 실행 오류", str(exc))
        except Exception:
            raise
