# YouTube Music Store (uv)

## 구성

- `collect.py`: YouTube Studio 음악 메타데이터를 `tracks.db`에 수집
- `music.py`: 장르별 미다운로드 곡 중 최대 30곡을 무작위 다운로드
- `pyproject.toml`: uv 프로젝트 및 의존성
- `tracks.db`: 실행 후 생성되는 SQLite DB
- `downloads/`: 내려받은 MP3 저장 폴더

## 준비

### 1. uv 설치 확인

```powershell
uv --version
```

### 2. Chrome을 원격 디버깅 모드로 실행

기존 Chrome 창을 모두 닫은 뒤 PowerShell 또는 CMD에서 실행합니다.

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%LOCALAPPDATA%\Google\Chrome\User Data"
```

Chrome에서 YouTube Studio에 로그인되어 있는지 확인하세요.

## 실행

압축을 푼 폴더에서:

```powershell
uv sync
uv run collect.py
```

수집이 끝나면:

```powershell
uv run music.py
```

`uv run`은 `pyproject.toml`의 의존성을 사용합니다. 별도로 `pip install`할 필요가 없습니다.

## 첫 실행 시

프로그램이 Studio 음악 페이지를 열고 요청을 기다립니다. 화면에서 음악 목록이 로드되지 않으면 Studio의 음악 페이지를 새로고침하세요. `music.py`는 `get_tracks` 요청을 캡처하기 위해 아무 곡의 Download 버튼을 한 번 누르도록 안내합니다. 해당 버튼 동작으로 브라우저에서 실제 다운로드가 시작될 수 있습니다.

## 주의

- `tracks.db`는 `collect.py`와 `music.py`가 같은 폴더에 있을 때 공유됩니다.
- 기존 DB 스키마가 다르다면 백업 후 `tracks.db`를 삭제하고 `uv run collect.py`로 다시 생성하세요.
- `downloadAudioUrl`은 만료될 수 있는 임시 URL이므로 저장하지 않습니다.
- 다운로드 기록은 `downloaded_tracks` 테이블에 남아 다음 실행에서 제외됩니다.
- YouTube Studio의 비공개 내부 API에 의존하므로 응답 구조나 인증 방식이 바뀌면 수정이 필요할 수 있습니다.
- 음악별 이용 조건과 출처 표기 요건을 확인한 뒤 사용하세요.
