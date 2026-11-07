# Music Library

외부 무료 음원 카탈로그와 직접 보유한 음원을 한 곳에서 관리하고, 원하는 길이의 배경음악 파일로 구성하는 개인용 음원 라이브러리입니다.

주요 기능은 음원 수집, 장르·무드별 분류, 목표 길이에 맞춘 믹스 생성, 구간별 타임라인 출력, 제외곡 관리입니다. 믹스 생성과 편집 외의 쇼츠 보조 기능은 포함하지 않습니다.

## 데스크톱 메뉴

명령어를 직접 입력하지 않고 사용하려면 다음 명령으로 선택형 메뉴 창을 여세요.

```bash
uv run music-crawler menu
```

창에는 **믹스 만들기**, **음원 라이브러리**, **믹스 검토 및 수정**, **사용 / 제외 목록**, **환경 및 폴더** 다섯 항목만 배치했습니다. 믹스 생성 화면에서 길이·장르·무드를 선택하고, 보유 음원만 사용할지 Jamendo·Openverse·YouTube Audio Library를 추가 조회할지 정할 수 있습니다. 긴 수집·렌더링 작업은 별도 작업 창에서 진행 상황을 표시합니다. 기존 CLI 명령어도 그대로 사용할 수 있습니다.

Windows/macOS Python 배포판에는 Tkinter가 일반적으로 포함되어 있습니다. Linux에서는 배포판에 따라 `python3-tk` 설치가 필요할 수 있습니다.

## 1. 준비

필수 환경은 Python 3.11 이상과 `ffmpeg`, `ffprobe`입니다.

```bash
uv sync
uv run music-crawler init
uv run music-crawler doctor
```

플랫폼을 사용할 때만 `.env`에 해당 인증 정보를 입력합니다.

- Jamendo: `JAMENDO_CLIENT_ID`
- Openverse: 인증 정보 없이도 제한적으로 동작하며, 더 높은 한도가 필요하면 `OPENVERSE_CLIENT_ID`, `OPENVERSE_CLIENT_SECRET`
- YouTube Audio Library: `pip install -e ".[youtube]"`, 로그인된 Chrome 원격 디버깅 세션 및 `YOUTUBE_CHANNEL_ID`

## 2. 폴더 사용법

`music-crawler init` 실행 후 생성되는 런타임 폴더만 사용하면 됩니다.

```text
workspace/
├── .env                       # 선택 플랫폼의 인증 정보
├── config/
│   └── profiles.toml          # 선택 수집 프로필(선택)
├── data/
│   └── music.db               # 곡 메타데이터, 사용 목록, ban 목록
├── library/
│   ├── inbox/                 # 직접 받은 음원을 넣는 곳
│   │   └── Jazz/Calm/song.mp3
│   ├── jamendo/               # 플랫폼에서 받은 음원
│   ├── openverse/
│   └── youtube/
└── output/
    └── playlists/             # 통합 음원과 구간 정보
```

직접 받은 곡은 `library/inbox/<장르>/<무드>/`에 넣습니다. 예를 들어 `library/inbox/Jazz/Calm/song.mp3`처럼 저장하면 폴더명을 장르와 무드 분류 힌트로 사용합니다. 파일 옆에 같은 이름의 JSON sidecar를 두면 제목, 아티스트, 라이선스, 출처 URL을 직접 지정할 수 있습니다.

## 3. 믹스 생성 방식 선택

### 보유 음원만 사용

외부 사이트를 조회하지 않고 `library/inbox`에 직접 넣은 음원만 사용합니다.

```bash
uv run music-crawler make 60 --genre jazz,newage --mood calm --library-only
```

### 외부 플랫폼을 선택해 자동 확장

우선 현재 라이브러리에서 곡을 고릅니다. 목표 길이에 부족할 때 지정한 플랫폼을 추가 조회하고, 새 후보를 반영해 길이를 다시 채웁니다. 실제 선택된 곡은 믹스 렌더링 단계에서 자동 다운로드됩니다.

```bash
uv run music-crawler make 60 --genre jazz,newage --mood calm --download-from jamendo,openverse
uv run music-crawler make 30 --genre piano,ambient --mood dreamy --download-from youtube
```

지원 플랫폼은 `jamendo`, `openverse`, `youtube`입니다. 여러 플랫폼은 쉼표로 구분합니다. YouTube는 로그인된 Chrome 세션이 필요합니다. 인증 정보가 없는 플랫폼이나 수집에 실패한 플랫폼은 경고를 남기고 다음 플랫폼을 계속 시도합니다.

무드 후보가 충분하지 않으면 먼저 선택한 곡은 유지하면서 같은 장르·출처 조건 안에서 다른 무드의 곡을 추가해 길이를 보충합니다. 그래도 길이가 부족하면 가능한 곡만으로 믹스를 만들고 부족한 길이를 알려줍니다.

`--download-from`과 `--library-only`는 함께 사용할 수 없습니다. 두 옵션을 생략하면 기존 DB 카탈로그를 사용합니다. DB에 저장된 외부 곡은 렌더링 시 파일이 없으면 다운로드합니다.

## 4. 편집 및 확정

```bash
uv run music-crawler show
uv run music-crawler remove 3 5       # 선택 곡 제거 + 자동 재충전 + 전역 BAN 등록
uv run music-crawler render           # 편집 결과로 다시 생성
uv run music-crawler commit           # 최종 사용 곡을 USED 목록에 등록
uv run music-crawler lists            # BAN / USED 목록 확인
uv run music-crawler unban jamendo:123 # BAN 해제
```

삭제한 곡은 기본적으로 전역 BAN 목록에 등록되어 이후 믹스에 다시 등장하지 않습니다. 현재 믹스에서만 제외하고 싶다면 `remove 3 --keep-eligible`를 사용합니다. 자동 재충전을 원하지 않으면 `--no-fill`을 추가할 수 있습니다.

최종 믹스를 확인한 뒤 `commit`해야 해당 곡들이 USED 목록에 등록됩니다. USED 곡은 기본적으로 다음 믹스 후보에서 제외됩니다.

## 5. 결과 파일

`output/playlists/<날짜_이름>/`에 생성됩니다.

- `mix.mp3`: 최종 통합 음원
- `tracks/`: 번호가 붙은 개별 곡 파일
- `playlist.m3u8`: 개별 곡 재생목록
- `description.txt`: 유튜브 설명란에 넣을 수 있는 구간별 곡 목록과 크레딧
- `timeline.csv`: 곡별 시작·종료 시각, 재생 길이(초), 출처 및 라이선스
- `credits.txt`: 저작자 표시가 필요한 곡의 크레딧
- `playlist.json`: 편집 가능한 플레이리스트 상태

곡 사이는 크로스페이드로 자연스럽게 연결하고, 마지막 곡에는 길이 자르기 여부와 관계없이 약 5초 페이드아웃을 적용합니다. `--exact`가 기본 활성화되어 마지막 곡을 잘라 목표 길이에 맞춥니다.

## 6. 라이브러리 관리

```bash
uv run music-crawler sync local
uv run music-crawler sync jamendo
uv run music-crawler sync openverse
uv run music-crawler sync youtube
uv run music-crawler stats --genre all --mood all
uv run music-crawler search "검색어"
uv run music-crawler ban jamendo:123
uv run music-crawler unban jamendo:123
uv run music-crawler lists
```

Jamendo는 태그별 커서를 저장해 다음 실행에서 이어서 수집합니다. 각 공급자는 라이선스, 오디오 길이, 보컬 정보 등을 확인해 후보에 등록합니다.

## 라이선스 주의

라이선스 자동 필터는 위험을 줄이는 보조 수단이지 사용 권리를 보증하지 않습니다. 게시 또는 수익화 전에 각 음원의 라이선스와 플랫폼 약관을 확인하세요. CC BY 등 저작자 표시가 필요한 음원은 생성된 `description.txt` 또는 `credits.txt`의 크레딧을 함께 사용해야 합니다. Jamendo API 및 YouTube Audio Library의 접근·사용 조건도 실제 사용 목적에 맞게 확인해야 합니다.
