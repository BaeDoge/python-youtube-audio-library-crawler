# music-crawler

잔잔한 연주곡(jazz · new age · classical · calm)을 라이선스 걱정 없이 모아서, **원하는 길이의 배경음악 mp3 하나**와 **유튜브 설명란 텍스트(곡 목록 + 크레딧)** 를 자동으로 만들어 주는 도구입니다.

*English: collects license-safe calm/instrumental music from Jamendo, Openverse, YouTube Audio Library and your own folder, then builds a fixed-length, crossfaded, loudness-normalised background-music mix with a ready-to-paste video description.*

## 빠른 시작

```bash
pip install -e .                  # 필요: Python 3.11+, ffmpeg(+ffprobe)
music-crawler init                # data/ library/ output/ 와 .env 생성
# .env 에 JAMENDO_CLIENT_ID 입력 (https://devportal.jamendo.com/)
music-crawler doctor              # 환경 점검
music-crawler sync                # Jamendo + Openverse + 내 폴더 수집
music-crawler stats               # 후보 풀: 장르/mood/보컬/라이선스별 곡 수, 만들 수 있는 플레이리스트 수
music-crawler make 60 --genre jazz,newage,classic --mood calm   # 60분 믹스 생성
```

`mc` 는 `music-crawler` 의 짧은 별칭입니다. YouTube 오디오 라이브러리까지 쓰려면 `pip install -e ".[youtube]"`.

## 작업 흐름

| 단계 | 명령 | 설명 |
|---|---|---|
| 수집 | `sync [jamendo\|openverse\|local\|youtube]` | 메타데이터만 수집. 제외된 곡은 **사유별 개수**를 보여줍니다. Jamendo 는 **이어서 진행**(아래) |
| 현황 | `stats --genre … --mood …` | 조건에 맞는 풀 크기와 "60분짜리 N개 가능" |
| 생성 | `make 60 …` | 곡 선택 → 다운로드 → 크로스페이드·음량 정규화 → `mix.mp3` |
| 편집 | `show`, `remove 3 5`, `add "검색어/id"`, `fill` | 삭제하면 목표 길이로 자동 재충전 (삭제한 곡은 다시 안 들어옴) |
| 재생성 | `render` | 편집 내용 반영 |
| 확정 | `commit` | 사용 처리 → 다음 영상에서는 제외 |
| 기타 | `ban`, `search`, `migrate-legacy` | 영구 제외 / 검색 / 이전 프로젝트 가져오기 |

길이는 `60`(분), `45:00`, `1:15:00` 형식. 기본은 마지막 곡을 페이드아웃으로 잘라 **정확히 그 길이**에 맞추고(`--no-exact` 로 끔), 곡 사이는 3초 크로스페이드, 곡 간 음량은 loudnorm 으로 맞춥니다.

결과: `output/playlists/<날짜_이름>/`

* `mix.mp3` — 합쳐진 최종 음원
* `tracks/01 - 아티스트 - 제목.mp3 …` — **곡별 개별 파일**. 번호가 `show`/`remove` 번호와 같아서, 들어 보고 별로인 곡을 `remove 3` 으로 뺄 수 있습니다 (`COPY_TRACKS=false` 로 끔)
* `playlist.m3u8` — 곡별 파일을 플레이어에서 한 번에 열기
* `description.txt`(타임스탬프 + CC BY 크레딧), `timeline.csv`, `credits.txt`, `playlist.json`

곡을 빼거나 넣은 뒤에는 `render` 를 다시 실행하면 `mix.mp3` 와 `tracks/` 가 새로 만들어집니다.

## Jamendo 수집은 이어서 진행됩니다

태그별 인기순 위치(커서)를 DB 에 저장해서, `sync jamendo` 를 다시 실행하면 **지난번 끝난 지점부터** 태그당 `--per-tag`(기본 400)곡을 더 확인합니다. 실행 비용이 카탈로그 크기와 무관하게 일정합니다. 매 실행마다 태그별 최신 등록곡 400개도 다시 확인해 새로 올라온 곡을 놓치지 않습니다.

* `sync jamendo --all` — 모든 태그를 끝까지 (중간에 Ctrl+C 해도 다음에 이어서)
* `sync jamendo --reset` — 위치를 지우고 처음부터

## 장르 고르기

* 기본값(`--genre` 생략)은 `relax` 프로필의 장르(Jazz, NewAge, Classical, Ambient, Piano, Acoustic, Lounge)와 mood(calm 계열)입니다.
* 다른 장르: `make 60 --genre rock --mood all` (프로필 mood 도 풀려면 `--mood all`).
* 장르 목록: Jazz NewAge Classical Ambient Piano Acoustic Lounge Lofi Cinematic Folk Blues Electronic Rock Pop HipHop RnB Reggae World Kids Holiday. mood: calm peaceful warm dreamy romantic melancholy uplifting dramatic dark energetic.
* 랜덤 장르: `make 60 --genre random`(곡이 충분한 장르 1개) / `--genre random:2`(2개 섞기). `--seed` 로 재현.
* 장르 풀 확인: `stats --genre all --mood all`.
* 새 장르를 **수집**하려면 `config/profiles.toml` 에 프로필을 만들고 `sync --profile 이름` (예시: `config/profiles.example.toml`). YouTube 오디오 라이브러리는 이미 전 장르 메타데이터를 가지고 있어 수집 없이 `--genre` 만 바꾸면 됩니다.

## 필터

* `--genre jazz,newage,classic` — 별칭 허용(classic→Classical). `all` 은 제한 없음.
* `--mood calm,peaceful` — mood 라벨이 없는 곡은 우선순위가 낮아질 뿐 제외되지는 않음. 완전 제외는 `--strict-mood`.
* `--vocals lenient`(기본, 보컬 확정곡만 제외) / `strict`(연주곡 확정만) / `any`.
  Jamendo 는 곡마다 보컬/연주 플래그가 있지만 **YouTube 오디오 라이브러리와 Openverse 는 없어서** `unknown` 으로 남습니다. `strict` 는 사실상 Jamendo 위주가 됩니다.
* 나만의 프로필: `config/profiles.example.toml` 참고.

## 내가 직접 받은 음원 (Pixabay 등)

`library/inbox/` 에 넣고 `music-crawler sync local`. 폴더명이 힌트가 됩니다: `inbox/Jazz/Calm/곡.mp3`. `inbox/pixabay/` 아래는 Pixabay 라이선스로 표시됩니다. 곡 옆에 같은 이름의 `.json`(title, artist, license, license_url, source_url …)을 두면 우선 적용됩니다. CC BY-NC 같은 비허용 라이선스는 자동으로 제외됩니다. 이 프로젝트는 Pixabay 를 자동 수집하지 않습니다 → [docs/SOURCES.md](docs/SOURCES.md).

## 이전 프로젝트에서 옮기기

```bash
music-crawler migrate-legacy /path/to/old/project
```

`tracks.db`, `used.txt`, 다운로드한 파일, 사용 이력을 새 구조로 가져오고 라이선스/mood 를 새 규칙으로 다시 계산합니다.

## 폴더 구조

```
src/music_crawler/   cli, config, db, taxonomy, selector, playlist, audio, downloader, providers/
tests/               pytest (ffmpeg 필요한 e2e 포함)
docs/                ARCHITECTURE.md, SOURCES.md
config/              profiles.example.toml
scripts/             chrome_debug.bat (YouTube 오디오 라이브러리용)
tools/shorts/        음악과 무관한 쇼츠 보조 스크립트
data/ library/ output/   ← 런타임 데이터, git 에 올라가지 않음
```

## 주의

라이선스 자동 필터는 위험을 줄일 뿐 권리를 보증하지 않습니다. 게시 전 [docs/SOURCES.md](docs/SOURCES.md) 를 읽고, CC BY 곡은 `description.txt` 의 크레딧을 설명란에 넣으세요.
