"""Pixabay music search-page downloader adapted for the music manager.

Uses the project's existing httpx dependency. Search results are discovered from
Pixabay HTML and detail pages; only real URLs on pixabay.com/cdn.pixabay.com are
accepted. No demo URLs or guessed MP3 URLs are ever returned.
"""
from __future__ import annotations

import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

PIXABAY_HOSTS = {"pixabay.com", "www.pixabay.com", "cdn.pixabay.com"}
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac")


@dataclass(frozen=True)
class PixabayTrack:
    title: str
    detail_url: str
    page: int
    track_id: str = ""
    download_url: str = ""


class _AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "a" and attrs.get("href"):
            self._current = {"href": attrs.get("href", ""), "title": attrs.get("title", ""), "text": []}
            self.anchors.append(self._current)
        elif self._current is not None and attrs.get("title") and not self._current["title"]:
            self._current["title"] = attrs["title"]

    def handle_data(self, data):
        if self._current is not None:
            self._current["text"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a":
            self._current = None


def _validate_pixabay_search_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in PIXABAY_HOSTS - {"cdn.pixabay.com"}:
        raise ValueError("Pixabay 검색 URL만 입력할 수 있습니다 (pixabay.com).")
    if "/music/search/" not in parsed.path:
        raise ValueError("음악 검색 URL이어야 합니다. 예: https://pixabay.com/music/search/new+age/")
    return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def _page_url(base_url: str, page: int) -> str:
    parsed = urlparse(base_url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "pagi"]
    if page > 1:
        query.append(("pagi", str(page)))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), ""))


def _normalize_audio_url(value: str, base_url: str) -> str | None:
    value = html.unescape(value).replace("\\/", "/").replace("\\u002F", "/")
    value = value.replace("\\u0026", "&").strip().rstrip("\\")
    if value.startswith("//"):
        value = "https:" + value
    value = urljoin(base_url, value)
    parsed = urlparse(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in PIXABAY_HOSTS:
        return None
    if not any(ext in parsed.path.lower() for ext in AUDIO_EXTENSIONS):
        return None
    return value


def _extract_audio_url(page_html: str, page_url: str) -> str | None:
    # Pixabay may place CDN URLs in serialized script data with escaped slashes.
    candidates = re.findall(
        r"(?:https?:)?(?:\\/\\/|//)?(?:cdn\\.pixabay\\.com|pixabay\\.com)[^\"'<>\\s\\\\]*?(?:\.mp3|\.m4a|\.wav|\.ogg|\.flac|\.aac)(?:\?[^\"'<>\\s]*)?",
        page_html,
        flags=re.IGNORECASE,
    )
    # A second, less restrictive pass handles JSON-escaped strings and URL-encoded query values.
    candidates += re.findall(r"[\"']([^\"']*(?:cdn\.pixabay\.com|pixabay\.com)[^\"']*\.(?:mp3|m4a|wav|ogg|flac|aac)(?:\?[^\"']*)?)[\"']", page_html, flags=re.I)
    for candidate in candidates:
        candidate = candidate.strip(" \t\r\n\"'")
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if candidate.startswith("cdn.pixabay.com"):
            candidate = "https://" + candidate
        found = _normalize_audio_url(candidate, page_url)
        if found:
            return found
    # Some HTML embeds an audio source directly.
    for candidate in re.findall(r"(?:src|href|data-url)\s*=\s*[\"']([^\"']+)[\"']", page_html, flags=re.I):
        found = _normalize_audio_url(candidate, page_url)
        if found:
            return found
    return None


def _looks_like_audio(data: bytes, content_type: str) -> bool:
    ctype = (content_type or "").lower()
    if ctype.startswith("audio/") or "mpeg" in ctype or "octet-stream" in ctype:
        pass
    elif not data:
        return False
    # Do not accept HTML error pages served with a misleading content type.
    sample = data[:512].lstrip().lower()
    if sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"{\"error")):
        return False
    if data.startswith((b"ID3", b"fLaC", b"OggS", b"RIFF", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\x00\x00\x00\x18ftyp", b"\x00\x00\x00\x20ftyp")):
        return True
    return ctype.startswith("audio/") and len(data) > 4096


class PixabayMusicDownloader:
    def __init__(self, *, timeout: float = 25.0, delay_seconds: float = 1.0, client=None):
        self.timeout = timeout
        self.delay_seconds = max(0.0, delay_seconds)
        self.client = client or httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
            },
        )
        self._owns_client = client is None
        self._browser_runtime = None
        self._browser = None
        self._browser_context = None
        self._browser_page = None
        self._browser_mode = False
        # music.py runs its interactive menu inside asyncio.run(main()). Playwright's
        # Sync API cannot run on that event-loop thread, so keep every Sync API call
        # (including cleanup) on one dedicated worker thread.
        self._browser_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pixabay-playwright")
        self._browser_executor_closed = False

    def close(self):
        if not self._browser_executor_closed:
            try:
                self._browser_executor.submit(self._close_browser_sync).result()
            finally:
                self._browser_executor.shutdown(wait=True, cancel_futures=True)
                self._browser_executor_closed = True
        if self._owns_client:
            self.client.close()

    def _close_browser_sync(self):
        if self._browser_runtime is not None:
            try:
                if self._browser_context is not None:
                    self._browser_context.close()
                if self._browser is not None:
                    self._browser.close()
            finally:
                self._browser_runtime.stop()
                self._browser_runtime = None
                self._browser = None
                self._browser_context = None
                self._browser_page = None

    def _get_text_via_browser(self, url: str) -> str:
        """Run synchronous Playwright in its own thread, even inside asyncio."""
        if self._browser_executor_closed:
            raise RuntimeError("Pixabay 브라우저 작업기가 이미 종료되었습니다. 다시 실행해 주세요.")
        return self._browser_executor.submit(self._get_text_via_browser_sync, url).result()

    def _get_text_via_browser_sync(self, url: str) -> str:
        """Use a normal, visible Chrome session if the direct HTTP request is blocked.

        This is a user-assisted fallback, not an attempt to bypass access controls:
        the user can inspect/complete any normal browser verification themselves.
        All calls to Playwright's Sync API are confined to the dedicated worker thread.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Pixabay가 일반 HTTP 요청을 403으로 거부했습니다. 브라우저 대체 모드에는 "
                "Playwright가 필요합니다. 프로젝트에서 `uv sync` 후 다시 실행해 주세요."
            ) from exc

        if self._browser_page is None:
            print("\n⚠️ Pixabay가 직접 HTTP 요청을 403으로 거부했습니다.")
            print("🌐 설치된 Chrome을 일반 창으로 열어 페이지를 다시 확인합니다.")
            self._browser_runtime = sync_playwright().start()
            try:
                self._browser = self._browser_runtime.chromium.launch(channel="chrome", headless=False)
            except Exception:
                try:
                    self._browser = self._browser_runtime.chromium.launch(headless=False)
                except Exception as exc:
                    self._browser_runtime.stop()
                    self._browser_runtime = None
                    raise RuntimeError(
                        "브라우저를 자동으로 열지 못했습니다. Chrome 설치 여부를 확인하거나 "
                        "`uv run playwright install chromium`을 실행한 뒤 다시 시도해 주세요."
                    ) from exc
            self._browser_context = self._browser.new_context(
                locale="ko-KR",
                viewport={"width": 1365, "height": 900},
            )
            self._browser_page = self._browser_context.new_page()

        first_browser_request = not getattr(self, "_browser_ready", False)
        try:
            response = self._browser_page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            self._browser_page.wait_for_timeout(1800)
        except Exception as exc:
            raise RuntimeError(f"브라우저에서 Pixabay 페이지를 열지 못했습니다: {exc}") from exc

        status = response.status if response is not None else None
        print(f"🌐 브라우저 응답 상태: {status if status is not None else '알 수 없음'}")
        if first_browser_request or status == 403:
            print("브라우저에서 페이지가 정상 표시되는지 확인하세요. 보안 확인 화면이 있다면 직접 완료한 뒤")
            input("콘솔에서 Enter를 누르면 현재 페이지 내용을 읽습니다 (중단하려면 Ctrl+C): ")
        content = self._browser_page.content()
        try:
            visible_text = self._browser_page.locator("body").inner_text(timeout=3000)
        except Exception:
            visible_text = ""
        lowered_body = visible_text.lower()
        lowered_html = content[:12000].lower()
        body_blocked_markers = ("403 forbidden", "access denied", "verify you are human", "complete the security check", "captcha")
        html_blocked_markers = ("403 forbidden", "access denied")
        if any(marker in lowered_body for marker in body_blocked_markers) or any(marker in lowered_html for marker in html_blocked_markers):
            raise RuntimeError(
                "Pixabay 페이지가 브라우저에서도 차단 상태입니다. 코드의 페이지 파싱 문제가 아니라 "
                "현재 접속에 대한 서버 거부입니다. 브라우저에서 페이지가 정상적으로 열리는지 먼저 확인해 주세요."
            )
        self._browser_ready = True
        return content

    def _get_text(self, url: str) -> str:
        if self._browser_mode:
            return self._get_text_via_browser(url)
        response = self.client.get(url)
        if getattr(response, "status_code", 200) == 403:
            self._browser_mode = True
            return self._get_text_via_browser(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type and b"<html" not in response.content[:1024].lower():
            raise RuntimeError(f"Pixabay 응답이 HTML이 아닙니다: {url}")
        return response.text

    def parse_page(self, url: str, page: int = 1) -> list[PixabayTrack]:
        page_url = _page_url(_validate_pixabay_search_url(url), page)
        content = self._get_text(page_url)
        parser = _AnchorParser()
        parser.feed(content)
        tracks: list[PixabayTrack] = []
        seen: set[str] = set()
        for anchor in parser.anchors:
            href = urljoin(page_url, html.unescape(anchor["href"]))
            parsed = urlparse(href)
            if (parsed.hostname or "").lower() not in {"pixabay.com", "www.pixabay.com"}:
                continue
            path = parsed.path
            if "/music/" not in path or "/music/search/" in path or "/music/download/" in path:
                continue
            match = re.search(r"-([0-9]+)/?$", path)
            if not match:
                continue
            canonical = urlunparse(("https", "pixabay.com", path.rstrip("/") + "/", "", "", ""))
            if canonical in seen:
                continue
            raw_title = " ".join(" ".join(anchor["text"]).split()) or anchor["title"].strip()
            if not raw_title or raw_title.casefold() in {"download", "preview", "play"}:
                continue
            seen.add(canonical)
            tracks.append(PixabayTrack(title=raw_title, detail_url=canonical, page=page, track_id=match.group(1)))
        return tracks

    def search(self, url: str, start_page: int = 1, end_page: int = 1) -> list[PixabayTrack]:
        url = _validate_pixabay_search_url(url)
        if start_page < 1 or end_page < start_page or end_page > 30:
            raise ValueError("페이지 범위는 1~30이고 시작 페이지는 종료 페이지 이하여야 합니다.")
        all_tracks: list[PixabayTrack] = []
        seen_ids: set[str] = set()
        for page in range(start_page, end_page + 1):
            if page > start_page and self.delay_seconds:
                time.sleep(self.delay_seconds)
            page_tracks = self.parse_page(url, page)
            for track in page_tracks:
                identity = track.track_id or track.detail_url
                if identity not in seen_ids:
                    seen_ids.add(identity)
                    all_tracks.append(track)
        return [PixabayTrack(t.title, t.detail_url, t.page, t.track_id, t.download_url) for t in all_tracks]

    def resolve_download_url(self, track: PixabayTrack) -> str:
        if track.download_url:
            found = _normalize_audio_url(track.download_url, track.detail_url)
            if found:
                return found
        detail_html = self._get_text(track.detail_url)
        audio_url = _extract_audio_url(detail_html, track.detail_url)
        if not audio_url:
            raise RuntimeError(f"상세 페이지에서 실제 오디오 URL을 찾지 못했습니다: {track.title}")
        return audio_url

    def download(self, track: PixabayTrack, destination_dir: str | Path) -> Path:
        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        audio_url = self.resolve_download_url(track)
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", track.title).strip(" ._") or f"track_{track.track_id}"
        prefix = f"pixabay_{track.track_id}_" if track.track_id else "pixabay_"
        target = destination_dir / f"{prefix}{safe_title}.mp3"
        if target.exists() and target.stat().st_size > 4096:
            return target
        temp = target.with_suffix(target.suffix + ".part")
        try:
            with self.client.stream("GET", audio_url, headers={"Referer": "https://pixabay.com/"}) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                chunks = []
                size = 0
                with temp.open("wb") as stream:
                    for chunk in response.iter_bytes(64 * 1024):
                        if chunk:
                            stream.write(chunk)
                            chunks.append(chunk[:512] if size == 0 else b"")
                            size += len(chunk)
                sample = b"".join(chunks)
                if size < 4096 or not _looks_like_audio(sample, content_type):
                    raise RuntimeError(f"다운로드 결과가 정상 오디오 파일로 확인되지 않았습니다 (bytes={size}, type={content_type})")
            os.replace(temp, target)
            return target
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def run_pixabay_download_flow(destination_dir: str | Path) -> None:
    """Interactive search and batch download flow for menu 10."""
    print("\n[Pixabay 음악 검색 및 일괄 다운로드]")
    default_url = "https://pixabay.com/music/search/new-age/"
    url = input(f"Pixabay 음악 검색 URL (Enter={default_url}): ").strip() or default_url
    try:
        start_page = int(input("시작 페이지 (기본 1): ").strip() or "1")
        end_page = int(input("종료 페이지 (기본 1, 최대 30): ").strip() or "1")
    except ValueError:
        print("❌ 페이지는 숫자로 입력해 주세요.")
        return
    downloader = PixabayMusicDownloader()
    try:
        print("검색 결과를 가져오는 중...")
        tracks = downloader.search(url, start_page, end_page)
        if not tracks:
            print("❌ 검색 결과를 찾지 못했습니다. Pixabay 페이지 구조나 네트워크 상태를 확인하세요. 데모 목록은 사용하지 않습니다.")
            return
        for index, track in enumerate(tracks, 1):
            print(f"{index:3}. {track.title} (페이지 {track.page}, ID {track.track_id})")
            print(f"     {track.detail_url}")
        print(f"\n총 {len(tracks)}곡을 찾았습니다.")
        raw = input(f"다운로드 범위 (예: 1-10, Enter=전체 목록): ").strip()
        if raw:
            match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", raw)
            if not match:
                print("❌ 범위 형식이 잘못되었습니다. 예: 1-10")
                return
            start, end = map(int, match.groups())
            if start < 1 or end < start or end > len(tracks):
                print(f"❌ 범위는 1-{len(tracks)} 사이여야 합니다.")
                return
            selected = tracks[start - 1:end]
        else:
            selected = tracks
        print(f"\n저장 위치: {destination_dir}")
        confirm = input(f"{len(selected)}곡을 다운로드할까요? (y/N): ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("다운로드를 취소했습니다.")
            return
        successes = 0
        failures = 0
        for index, track in enumerate(selected, 1):
            try:
                path = downloader.download(track, destination_dir)
                print(f"✅ [{index}/{len(selected)}] {track.title} -> {path.name}")
                successes += 1
            except Exception as exc:
                print(f"❌ [{index}/{len(selected)}] {track.title}: {exc}")
                failures += 1
            if index < len(selected) and downloader.delay_seconds:
                time.sleep(downloader.delay_seconds)
        print(f"\n다운로드 완료: {successes} 성공 / {failures} 실패")
        if successes:
            print("메뉴 11에서 카탈로그를 갱신하면 다운로드한 음원을 플레이리스트에 사용할 수 있습니다.")
    except Exception as exc:
        print(f"❌ Pixabay 검색 실패: {exc}")
    finally:
        downloader.close()
