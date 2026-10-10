# YouTube Music Store

YouTube Studio Audio Library의 음악 메타데이터를 수집하고, 조건에 맞는 음악을 선택하여 다운로드한 뒤 YouTube 영상용 플레이리스트 패키지를 만드는 Python 프로젝트입니다.

현재 `music.py`는 영상 길이를 입력하면 해당 길이에 최대한 맞는 음악 조합을 자동으로 구성합니다.

## 주요 기능

- YouTube Studio Audio Library 음악 메타데이터 수집
- 장르(Genre)와 Mood 기반 필터링
- 이미 다운로드했거나 사용한 음악 제외
- 영상 길이에 맞춘 플레이리스트 자동 구성
- 음악 길이와 장르/Mood 등의 조건을 고려한 조합 선택
- 아티스트 반복을 줄이는 곡 배치
- 선택된 음악만 MP3로 다운로드
- 플레이리스트별 개별 음원 패키지 생성
- FFmpeg를 이용한 전체 음악 통합 MP3 생성
- YouTube 설명란용 음악 목록 자동 생성
- `playlist.json` 메타데이터 생성
- 초안(Draft) 저장 후 마지막 플레이리스트를 Commit하는 방식의 사용 이력 관리

## 프로젝트 구성

- `collect.py`: YouTube Studio Audio Library의 음악 메타데이터를 `tracks.db`에 수집
- `music.py`: 장르/Mood/사용 이력/영상 길이를 기준으로 플레이리스트를 구성하고 음악을 다운로드
- `pyproject.toml`: Python 프로젝트 및 의존성 설정
- `tracks.db`: 음악 메타데이터와 다운로드/사용 이력을 저장하는 SQLite DB
- `downloads/`: 다운로드된 원본 MP3 저장
- `playlists/`: 생성된 플레이리스트별 결과물
- `playlist_drafts/`: 아직 Commit하지 않은 플레이리스트 초안
- `used.txt`: 기존에 사용한 곡 제목을 가져오기 위한 레거시 파일

## 요구사항

- Windows / Python 3.11+
- `uv`
- Google Chrome
- 로그인된 YouTube Studio 계정
- FFmpeg
- 인터넷 연결

Python 패키지는 `uv sync`를 통해 설치합니다.

## 설치

### 1. uv 설치 확인

```powershell
uv --version
```

### 2. 의존성 설치

프로젝트 폴더에서:

```powershell
uv sync
```

별도의 `pip install`은 필요하지 않습니다.

## FFmpeg 설치

`music.py`는 개별 MP3 다운로드와 별개로, 선택된 여러 곡을 하나의 `background_music.mp3`로 합칠 때 FFmpeg를 사용합니다.

FFmpeg가 없어도 개별 MP3 다운로드와 플레이리스트 패키지 생성은 진행되지만, 통합 MP3는 생성되지 않습니다.

실행 전 다음 명령으로 확인할 수 있습니다.

```powershell
ffmpeg -version
```

정상적으로 설치되어 있다면 FFmpeg 버전 정보가 출력됩니다.

### Windows에서 FFmpeg 사용

FFmpeg 실행 파일을 설치한 뒤 `ffmpeg.exe`가 PATH에 등록되어 있어야 합니다.

PATH 등록이 어려운 경우 `.env`에서 FFmpeg 실행 파일의 전체 경로를 직접 지정할 수 있습니다.

```env
FFMPEG_BIN=C:\ffmpeg\bin\ffmpeg.exe
```

기본값은 다음과 같습니다.

```env
FFMPEG_BIN=ffmpeg
```

따라서 PATH에 `ffmpeg`가 등록되어 있으면 별도 설정이 필요하지 않습니다.

## Chrome 실행

이 프로젝트는 YouTube Studio의 로그인 세션을 사용하기 때문에, 일반적인 새 Chrome 프로세스가 아니라 원격 디버깅이 활성화된 Chrome에 연결합니다.

기존 Chrome 창을 모두 닫은 뒤 PowerShell 또는 CMD에서 실행합니다.

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%LOCALAPPDATA%\Google\Chrome\User Data"
```

Chrome에서 YouTube Studio에 로그인되어 있는지 확인합니다.

## 첫 실행

먼저 음악 메타데이터를 수집합니다.

```powershell
uv run collect.py
```

수집이 완료되면:

```powershell
uv run music.py
```

`music.py`가 실행되면 YouTube Studio의 음악 페이지에서 아무 곡의 **Download** 버튼을 한 번 눌러 `get_tracks` 요청을 캡처합니다.

이 요청은 실제 다운로드 URL을 얻기 위한 내부 API 요청이며, 현재 Chrome의 로그인 세션을 사용하는 방식으로 호출됩니다.

### 중요

`get_tracks` API는 로그인된 YouTube Studio 세션이 필요합니다.

따라서 다음과 같은 오류가 발생한다면:

```text
401 Unauthorized

You must be signed in to perform this operation.
```

Chrome이 YouTube Studio에 로그인되어 있는지 먼저 확인해야 합니다.

## 플레이리스트 생성 과정

`music.py`에서 `1. 새 플레이리스트 만들기`를 선택하면 다음 순서로 진행됩니다.

1. 영상 길이 입력
2. 장르 선택
3. Mood 선택
4. 사용하지 않은 곡 필터링
5. 영상 길이에 맞는 음악 조합 계산
6. 선택된 곡 목록 확인
7. 영상 제목 입력
8. 선택된 음악만 다운로드
9. 플레이리스트 폴더에 개별 음원 복사
10. FFmpeg로 통합 MP3 생성
11. YouTube 설명란 생성
12. `playlist.json` 생성
13. Draft 저장

예를 들어 영상 길이를:

```text
30:00
```

으로 입력하면 프로그램이 해당 시간에 최대한 가까운 음악 조합을 찾습니다.

## 결과 폴더

플레이리스트가 생성되면 다음과 같은 구조가 만들어집니다.

```text
playlists/
└── 20261002_103000_제주도_해안도로/
    ├── tracks/
    │   ├── 01 - Song A.mp3
    │   ├── 02 - Song B.mp3
    │   ├── 03 - Song C.mp3
    │   └── ...
    │
    ├── background_music.mp3
    ├── youtube_description.txt
    └── playlist.json
```

### `background_music.mp3`

모든 선택 곡을 순서대로 하나의 MP3 파일로 합친 파일입니다.

FFmpeg가 설치되어 있지 않거나 실행에 실패하면 이 파일만 생성되지 않으며, 개별 음원은 그대로 보존됩니다.

프로그램에는 다음과 같은 안내가 표시됩니다.

```text
⚠️ FFmpeg를 찾지 못했습니다.
통합 MP3는 만들지 않고 개별 음원만 보존합니다.
```

## 사용 이력

다운로드한 음악은 `downloaded_tracks` 테이블에 기록됩니다.

따라서 다음 실행에서는 이미 다운로드한 곡이 자동으로 제외됩니다.

기존에 다른 방법으로 사용했던 곡은 `used.txt`를 이용해 가져올 수 있습니다.

```text
Song Title 1
Song Title 2
Song Title 3
```

프로그램 메뉴의:

```text
7. 기존 used.txt 가져오기
```

를 이용하면 됩니다.

## 메뉴

현재 `music.py`의 메뉴는 다음과 같습니다.

```text
1. 새 플레이리스트 만들기
2. 마지막 플레이리스트 Commit
3. 장르 목록
4. Mood 목록
5. 음악 사용 이력
6. 플레이리스트 이력
7. 기존 used.txt 가져오기
0. 종료
```

### 1. 새 플레이리스트 만들기

영상 길이, 장르, Mood를 입력하여 새로운 플레이리스트를 생성합니다.

### 2. 마지막 플레이리스트 Commit

검토가 끝난 마지막 Draft를 실제 사용 이력에 반영합니다.

음원을 먼저 들어보고 문제가 없을 때 Commit하는 것을 권장합니다.

### 3. 장르 목록

현재 DB에 수집된 장르를 확인합니다.

### 4. Mood 목록

현재 DB에 수집된 Mood를 확인합니다.

### 5. 음악 사용 이력

다운로드 및 사용 이력을 확인합니다.

### 6. 플레이리스트 이력

생성된 플레이리스트 이력을 확인합니다.

### 7. 기존 used.txt 가져오기

기존에 사용했던 곡 제목 목록을 DB의 사용 이력으로 가져옵니다.

## 주요 환경변수

`.env`에서 주요 동작을 변경할 수 있습니다.

```env
# Chrome CDP
CDP_URL=http://127.0.0.1:9222

# 다운로드 폴더
DOWNLOAD_DIR=downloads

# 플레이리스트 결과 폴더
PLAYLIST_DIR=playlists

# Draft 폴더
DRAFT_DIR=playlist_drafts

# 기존 사용 곡 목록
USED_FILE=used.txt

# FFmpeg
FFMPEG_BIN=ffmpeg

# 플레이리스트 최대 곡 수
MAX_TRACKS_PER_PLAYLIST=12

# 최소 곡 길이
MIN_TRACK_DURATION_SECONDS=120

# 최대 곡 길이
MAX_TRACK_DURATION_SECONDS=0

# 선호 곡 길이
PREFERRED_TRACK_DURATION_SECONDS=240
```

`MAX_TRACK_DURATION_SECONDS=0`은 최대 곡 길이 제한을 사용하지 않는다는 의미입니다.

## FFmpeg 통합 과정

통합 MP3는 선택된 음악 파일들을 FFmpeg concat 기능으로 합친 후 MP3로 인코딩합니다.

현재 출력 설정은:

```text
MP3
192 kbps
ID3v2.3
```

입니다.

FFmpeg가 정상적으로 실행되면 다음과 같이 표시됩니다.

```text
🎧 통합 MP3 생성 중...
✅ 통합 MP3: ...\background_music.mp3
```

## 데이터베이스

`tracks.db`에는 YouTube Studio에서 수집한 음악 메타데이터와 다운로드/사용 이력이 저장됩니다.

`collect.py`와 `music.py`는 동일한 `tracks.db`를 사용해야 합니다.

DB 구조를 변경한 경우 기존 DB를 백업한 뒤 삭제하고 다시 수집할 수 있습니다.

```powershell
uv run collect.py
```

## 주의사항

- YouTube Studio에 로그인된 Chrome 세션이 필요합니다.
- `get_tracks`는 YouTube Studio의 비공개 내부 API에 의존합니다.
- YouTube의 API 또는 인증 방식이 변경되면 코드 수정이 필요할 수 있습니다.
- `downloadAudioUrl`은 만료될 수 있는 임시 URL이므로 저장하지 않습니다.
- FFmpeg가 없어도 개별 음원 다운로드는 가능하지만 통합 MP3는 생성되지 않습니다.
- FFmpeg가 설치되어 있어도 PATH 또는 `FFMPEG_BIN` 설정이 잘못되면 통합 MP3 생성에 실패할 수 있습니다.
- 음악별 이용 조건 및 출처 표기 요건을 확인한 뒤 사용하세요.
- Chrome 로그인 정보나 인증 쿠키 등 계정 인증 정보는 Git에 커밋하지 마세요.

## 실행 요약

```powershell
# 의존성 설치
uv sync

# 음악 메타데이터 수집
uv run collect.py

# 플레이리스트 생성 및 다운로드
uv run music.py
```

FFmpeg 확인:

```powershell
ffmpeg -version
```

# 외부 음원 연동

## 1. 직접 다운로드한 음원 우선 사용

외부 음원 등록/업데이트 메뉴는 YouTube 다운로드 인증을 먼저 캡처하지 않아도 실행할 수 있습니다. YouTube Audio Library가 필요한 메뉴(새 플레이리스트 생성, 싫은 곡 제거)를 처음 선택할 때만 Chrome/Studio 인증을 요청합니다.

1. 프로젝트 루트에 `external_music` 폴더를 만듭니다.
2. 직접 다운로드한 MP3/WAV/FLAC/OGG/M4A 파일을 넣습니다. 하위 폴더도 검색합니다.
3. 권장 폴더 구조는 `external_music/장르/Mood/음원.mp3` 입니다. 예: `external_music/Jazz/Calm/song.mp3`
4. 메뉴에서 `9. 외부 음원 메타데이터 업데이트 + 내 폴더 음원 등록`을 선택합니다.
5. FFprobe가 설치되어 있으면 파일 길이를 자동으로 읽습니다. 길이를 확인하지 못한 파일은 등록을 건너뜁니다.
6. 로컬 음원은 장르/Mood 필터에 관계없이 후보에 포함되고, 선택 점수에서 우선됩니다. 단, 전체 길이를 더 잘 맞추는 조합이 우선이므로 모든 로컬 음원을 무조건 다 넣는 것은 아닙니다.

선택 사항으로 음원 옆에 같은 파일명의 JSON을 둘 수 있습니다. 예를 들어 `song.mp3`와 `song.json`:

```json
{
  "title": "Song title",
  "artist": "Artist name",
  "genre": "Jazz",
  "mood": "Calm",
  "duration": 210,
  "license_type": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "source_url": "https://example.com/track-page"
}
```

직접 받은 음원의 라이선스는 프로그램이 자동으로 검증할 수 없으므로 게시 전에 확인해야 합니다. 출처 정보가 필요한 음원은 JSON에 `license_url`과 `source_url`을 적어 두세요.

## 2. 온라인 카탈로그 메타데이터 업데이트

현재 구현된 온라인 카탈로그 공급자는 Jamendo API입니다.

1. Jamendo 개발자 포털에서 앱/client ID를 발급합니다: https://devportal.jamendo.com/
2. `.env`에 `JAMENDO_CLIENT_ID=발급받은_ID`를 설정합니다.
3. 프로그램 메뉴에서 9번을 선택하고, 온라인 카탈로그 업데이트에 `y`를 입력합니다.
4. 조건에 맞는 메타데이터는 `external_music_catalog.json`에 저장되며 `tracks.db`에도 등록됩니다.
5. 플레이리스트에 선택된 온라인 음원은 필요한 시점에 다운로드됩니다. 전체 카탈로그의 오디오 파일을 한꺼번에 내려받지는 않습니다.

자동 필터는 CC BY 및 CC0 라이선스만 허용하고, 다운로드 허용 플래그가 있는 곡만 등록합니다. 단, API의 사용 조건과 상업적 이용 조건은 별개입니다. Jamendo API 약관은 상업적 이용에 별도 허가가 필요할 수 있으므로, 수익화 채널에서 API를 쓰기 전에 Jamendo의 최신 약관/허가를 확인해야 합니다. 허가가 확인되지 않으면 온라인 카탈로그 기능 대신 라이선스를 직접 확인해 받은 음원을 `external_music`에 넣는 방식을 사용하세요.

## 3. 사용 처리

플레이리스트를 검토한 뒤 `2. 마지막 플레이리스트 Commit`을 선택하면 기존 DB 사용 이력에 기록되고, `used.txt`에도 `아티스트 - 제목` 형식으로 추가됩니다. 이후 후보에서 제외됩니다.
