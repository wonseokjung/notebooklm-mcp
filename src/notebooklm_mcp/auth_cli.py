#!/usr/bin/env python3
"""CLI tool to authenticate with NotebookLM MCP.

This tool connects to Chrome via DevTools Protocol, navigates to NotebookLM,
and extracts authentication tokens. If the user is not logged in, it waits
for them to log in via the Chrome window.

Usage:
    1. Start Chrome with remote debugging:
       /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222

    2. Or, if Chrome is already running, it may already have debugging enabled.

    3. Run this tool:
       notebooklm-mcp-auth

    4. If not logged in, log in via the Chrome window

    5. Tokens are cached to ~/.notebooklm-mcp/auth.json
"""

import json
import re
import sys
import time
from pathlib import Path

import httpx

from .auth import (
    AuthTokens,
    REQUIRED_COOKIES,
    extract_csrf_from_page_source,
    get_cache_path,
    save_tokens_to_cache,
    validate_cookies,
)


CDP_DEFAULT_PORT = 9222
NOTEBOOKLM_URL = "https://notebooklm.google.com/"


def get_chrome_user_data_dir() -> str | None:
    """Get the default Chrome user data directory."""
    import platform
    from pathlib import Path

    system = platform.system()
    home = Path.home()

    if system == "Darwin":
        return str(home / "Library/Application Support/Google/Chrome")
    elif system == "Linux":
        return str(home / ".config/google-chrome")
    elif system == "Windows":
        return str(home / "AppData/Local/Google/Chrome/User Data")
    return None


def launch_chrome(port: int, headless: bool = False) -> "subprocess.Popen | None":
    """Launch Chrome with remote debugging enabled.

    Args:
        port: The debugging port to use
        headless: If True, launch in headless mode (no visible window)

    Returns:
        Popen process handle if Chrome was launched, None if failed
    """
    import platform
    import subprocess

    system = platform.system()

    if system == "Darwin":
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    elif system == "Linux":
        # Try multiple Chrome binary names (varies by distro)
        import shutil
        chrome_candidates = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
        chrome_path = None
        for candidate in chrome_candidates:
            if shutil.which(candidate):
                chrome_path = candidate
                break
        if not chrome_path:
            print(f"Chrome not found. Tried: {', '.join(chrome_candidates)}")
            return None
    elif system == "Windows":
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    else:
        print(f"Unsupported platform: {system}")
        return None

    # Chrome 136+ requires a non-default user-data-dir for remote debugging
    # We use a persistent directory so Google login is remembered across runs
    profile_dir = Path.home() / ".notebooklm-mcp" / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",  # Bypass extensions that may interfere (e.g., Antigravity IDE)
        f"--user-data-dir={profile_dir}",  # Persistent profile for login persistence
        "--remote-allow-origins=*",  # Allow WebSocket connections from any origin
    ]

    if headless:
        args.append("--headless=new")

    try:
        # Print the command for debugging
        print(f"Running: {' '.join(args[:3])}...")
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(3)  # Wait for Chrome to start

        # Check if there was an immediate error
        if process.poll() is not None:
            _, stderr = process.communicate()
            if stderr:
                print(f"Chrome error: {stderr.decode()[:500]}")
            return None
        return process
    except Exception as e:
        print(f"Failed to launch Chrome: {e}")
        return None


def get_chrome_debugger_url(port: int = CDP_DEFAULT_PORT) -> str | None:
    """Get the WebSocket debugger URL for Chrome."""
    try:
        response = httpx.get(f"http://localhost:{port}/json/version", timeout=5)
        data = response.json()
        return data.get("webSocketDebuggerUrl")
    except Exception:
        return None


def get_chrome_pages(port: int = CDP_DEFAULT_PORT) -> list[dict]:
    """Get list of open pages in Chrome."""
    try:
        response = httpx.get(f"http://localhost:{port}/json", timeout=5)
        return response.json()
    except Exception:
        return []


def find_or_create_notebooklm_page(port: int = CDP_DEFAULT_PORT) -> dict | None:
    """Find an existing NotebookLM page or create a new one."""
    from urllib.parse import quote

    pages = get_chrome_pages(port)

    # Look for existing NotebookLM page
    for page in pages:
        url = page.get("url", "")
        if "notebooklm.google.com" in url:
            return page

    # Create a new page - URL must be properly encoded
    try:
        encoded_url = quote(NOTEBOOKLM_URL, safe="")
        response = httpx.put(
            f"http://localhost:{port}/json/new?{encoded_url}",
            timeout=15
        )
        if response.status_code == 200 and response.text.strip():
            return response.json()

        # Fallback: create blank page then navigate
        response = httpx.put(f"http://localhost:{port}/json/new", timeout=10)
        if response.status_code == 200 and response.text.strip():
            page = response.json()
            # Navigate to NotebookLM using the page's websocket
            ws_url = page.get("webSocketDebuggerUrl")
            if ws_url:
                navigate_to_url(ws_url, NOTEBOOKLM_URL)
            return page

        print(f"Failed to create page: status={response.status_code}")
        return None
    except Exception as e:
        print(f"Failed to create new page: {e}")
        return None


def execute_cdp_command(ws_url: str, method: str, params: dict = None) -> dict:
    """Execute a CDP command via WebSocket."""
    import websocket

    ws = websocket.create_connection(ws_url, timeout=30)
    try:
        command = {
            "id": 1,
            "method": method,
            "params": params or {}
        }
        ws.send(json.dumps(command))

        # Wait for response
        while True:
            response = json.loads(ws.recv())
            if response.get("id") == 1:
                return response.get("result", {})
    finally:
        ws.close()


def get_page_cookies(ws_url: str) -> list[dict]:
    """Get all cookies for the page."""
    result = execute_cdp_command(ws_url, "Network.getCookies")
    return result.get("cookies", [])


def get_page_html(ws_url: str) -> str:
    """Get the page HTML to extract CSRF token."""
    # Enable Runtime domain
    execute_cdp_command(ws_url, "Runtime.enable")

    # Execute JavaScript to get page HTML
    result = execute_cdp_command(
        ws_url,
        "Runtime.evaluate",
        {"expression": "document.documentElement.outerHTML"}
    )

    return result.get("result", {}).get("value", "")


def navigate_to_url(ws_url: str, url: str) -> None:
    """Navigate the page to a URL."""
    execute_cdp_command(ws_url, "Page.enable")
    execute_cdp_command(ws_url, "Page.navigate", {"url": url})
    # Wait for page to load
    time.sleep(3)


def get_current_url(ws_url: str) -> str:
    """Get the current page URL via CDP (cheap operation, no JS evaluation)."""
    execute_cdp_command(ws_url, "Runtime.enable")
    result = execute_cdp_command(
        ws_url,
        "Runtime.evaluate",
        {"expression": "window.location.href"}
    )
    return result.get("result", {}).get("value", "")


def check_if_logged_in_by_url(url: str) -> bool:
    """Check login status by URL - much cheaper than parsing HTML.

    If NotebookLM redirects to accounts.google.com, user is not logged in.
    If URL stays on notebooklm.google.com, user is authenticated.
    """
    if "accounts.google.com" in url:
        return False
    if "notebooklm.google.com" in url:
        return True
    # Unknown URL - assume not logged in
    return False


def extract_session_id_from_html(html: str) -> str:
    """Extract session ID from page HTML."""
    patterns = [
        r'"FdrFJe":"(\d+)"',
        r'f\.sid["\s:=]+["\']?(\d+)',
        r'"cfb2h":"([^"]+)"',
    ]

    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)

    return ""


def is_chrome_profile_locked(profile_dir: str | None = None) -> bool:
    """Check if a Chrome profile is locked (Chrome is using it).

    Args:
        profile_dir: The profile directory to check. If None, checks our
                     notebooklm-mcp profile, NOT the default Chrome profile.

    This is more reliable than process detection because:
    - Works across all platforms
    - Detects if Chrome is using the specific profile we need
    - The lock file only exists while Chrome has the profile open
    """
    if profile_dir is None:
        # Check OUR profile, not the default Chrome profile
        # We use a separate profile so we can run alongside the user's main Chrome
        profile_dir = str(Path.home() / ".notebooklm-mcp" / "chrome-profile")

    # Chrome creates a "SingletonLock" file when the profile is in use
    lock_file = Path(profile_dir) / "SingletonLock"
    return lock_file.exists()


def is_our_chrome_profile_in_use() -> bool:
    """Check if OUR Chrome profile is already in use.

    We use a separate profile at ~/.notebooklm-mcp/chrome-profile
    so we can run alongside the user's main Chrome browser.

    This only checks if our specific profile is locked, NOT if Chrome
    is running in general. Users can have their main Chrome open.
    """
    return is_chrome_profile_locked()  # Already checks our profile by default


def has_chrome_profile() -> bool:
    """Check if a Chrome profile with saved login exists.
    
    Returns True if the profile directory exists and has login cookies,
    indicating that the user has previously authenticated.
    """
    profile_dir = Path.home() / ".notebooklm-mcp" / "chrome-profile"
    # Check for Cookies file which indicates the profile has been used
    cookies_file = profile_dir / "Default" / "Cookies"
    return cookies_file.exists()


def run_headless_auth(port: int = 9223, timeout: int = 30) -> AuthTokens | None:
    """Run authentication in headless mode (no user interaction).
    
    This only works if the Chrome profile already has saved Google login.
    The Chrome process is automatically terminated after token extraction.
    
    Args:
        port: Chrome DevTools port (use different port to avoid conflicts)
        timeout: Maximum time to wait for auth extraction
        
    Returns:
        AuthTokens if successful, None if failed or no saved login
    """
    import subprocess
    
    # Check if profile exists with saved login
    if not has_chrome_profile():
        return None
    
    # Check if our profile is already in use
    if is_our_chrome_profile_in_use():
        return None
    
    chrome_process: subprocess.Popen | None = None
    try:
        # Launch Chrome in headless mode
        chrome_process = launch_chrome(port, headless=True)
        if not chrome_process:
            return None
        
        # Wait for Chrome debugger to be ready
        debugger_url = None
        for _ in range(5):  # Try up to 5 times
            debugger_url = get_chrome_debugger_url(port)
            if debugger_url:
                break
            time.sleep(1)
        
        if not debugger_url:
            return None
        
        # Find or create NotebookLM page
        page = find_or_create_notebooklm_page(port)
        if not page:
            return None
        
        ws_url = page.get("webSocketDebuggerUrl")
        if not ws_url:
            return None
        
        # Check if logged in by URL
        current_url = get_current_url(ws_url)
        if not check_if_logged_in_by_url(current_url):
            # Not logged in - headless can't help
            return None
        
        # Extract cookies
        cookies_list = get_page_cookies(ws_url)
        cookies = {c["name"]: c["value"] for c in cookies_list}
        
        if not validate_cookies(cookies):
            return None
        
        # Get page HTML for CSRF extraction
        html = get_page_html(ws_url)
        csrf_token = extract_csrf_from_page_source(html)
        session_id = extract_session_id_from_html(html)
        
        # Create and save tokens
        tokens = AuthTokens(
            cookies=cookies,
            csrf_token=csrf_token or "",
            session_id=session_id or "",
            extracted_at=time.time(),
        )
        save_tokens_to_cache(tokens)
        
        return tokens
        
    except Exception:
        return None
        
    finally:
        # IMPORTANT: Always terminate headless Chrome
        if chrome_process:
            try:
                chrome_process.terminate()
                chrome_process.wait(timeout=5)
            except Exception:
                # Force kill if terminate didn't work
                try:
                    chrome_process.kill()
                except Exception:
                    pass



def run_auth_flow(port: int = CDP_DEFAULT_PORT, auto_launch: bool = True) -> AuthTokens | None:
    """Run the authentication flow.

    Args:
        port: Chrome DevTools port
        auto_launch: If True, automatically launch Chrome if not running
    """
    print("NotebookLM MCP 인증")
    print("=" * 40)
    print()

    # Track Chrome process so we can close it after auth
    chrome_process = None
    
    # Check if Chrome is running with debugging
    debugger_url = get_chrome_debugger_url(port)

    if not debugger_url and auto_launch:
        # Check if our specific profile is already in use
        if is_our_chrome_profile_in_use():
            print("NotebookLM 인증 프로필을 이미 사용 중입니다.")
            print()
            print("이전 인증 Chrome 창이 아직 열려 있다는 뜻입니다.")
            print("해당 창을 닫고 다시 시도하거나, 파일 모드를 사용하세요:")
            print()
            print("  notebooklm-mcp-auth --file")
            print()
            return None

        # We can launch our separate Chrome profile even if user's main Chrome is open
        print("NotebookLM 인증 프로필로 Chrome을 실행 중입니다...")
        print("(처음인 경우: Google 계정에 로그인해야 합니다)")
        print()
        # Launch with visible window so user can log in
        chrome_process = launch_chrome(port, headless=False)
        time.sleep(3)
        debugger_url = get_chrome_debugger_url(port)

    if not debugger_url:
        print(f"오류: Chrome 포트 {port}에 연결할 수 없습니다")
        print()
        print("다음과 같은 경우 발생할 수 있습니다:")
        print("  - Chrome을 시작하지 못했습니다")
        print("  - 다른 프로세스가 9222 포트를 사용 중입니다")
        print("  - 방화벽이 포트를 차단하고 있습니다")
        print()
        print("시도: 대신 파일 모드를 사용하세요 (가장 안정적):")
        print("     notebooklm-mcp-auth --file")
        print()
        return None

    print(f"Chrome 디버거에 연결되었습니다")

    # Find or create NotebookLM page
    page = find_or_create_notebooklm_page(port)
    if not page:
        print("오류: NotebookLM 페이지를 찾거나 생성하는 데 실패했습니다")
        return None

    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        print("오류: 페이지의 WebSocket URL이 없습니다")
        return None

    print(f"사용 중인 페이지: {page.get('title', 'Unknown')}")

    # Navigate to NotebookLM if needed
    current_url = page.get("url", "")
    if "notebooklm.google.com" not in current_url:
        print("NotebookLM으로 이동 중...")
        navigate_to_url(ws_url, NOTEBOOKLM_URL)

    # Check login status by URL (cheap - no HTML parsing)
    print("로그인 상태 확인 중...")
    current_url = get_current_url(ws_url)

    if not check_if_logged_in_by_url(current_url):
        print()
        print("=" * 40)
        print("로그인되지 않음")
        print("=" * 40)
        print()
        print("Chrome 창에서 NotebookLM에 로그인해 주세요.")
        print("로그인이 완료될 때까지 기다립니다...")
        print()
        print("(취소하려면 Ctrl+C를 누르세요)")
        print()

        # Wait for login - check URL every 5 seconds (cheap operation)
        max_wait = 300  # 5 minutes
        start_time = time.time()
        while time.time() - start_time < max_wait:
            time.sleep(5)
            try:
                current_url = get_current_url(ws_url)
                if check_if_logged_in_by_url(current_url):
                    print("로그인이 감지되었습니다!")
                    break
            except Exception as e:
                print(f"대기 중... ({e})")

        if not check_if_logged_in_by_url(current_url):
            print("오류: 로그인 시간 초과. 다시 시도해 주세요.")
            return None

    # Extract cookies
    print("쿠키 추출 중...")
    cookies_list = get_page_cookies(ws_url)
    cookies = {c["name"]: c["value"] for c in cookies_list}

    if not validate_cookies(cookies):
        print("오류: 필수 쿠키가 누락되었습니다. 완전히 로그인되었는지 확인해 주세요.")
        print(f"Required: {REQUIRED_COOKIES}")
        print(f"Found: {list(cookies.keys())}")
        return None

    # Get page HTML for CSRF extraction
    html = get_page_html(ws_url)

    # Extract CSRF token
    print("CSRF 토큰 추출 중...")
    csrf_token = extract_csrf_from_page_source(html)
    if not csrf_token:
        print("경고: 페이지에서 CSRF 토큰을 추출할 수 없습니다.")
        print("네트워크 탭에서 수동으로 추출해야 할 수도 있습니다.")
        csrf_token = ""

    # Extract session ID
    session_id = extract_session_id_from_html(html)

    # Create tokens object
    tokens = AuthTokens(
        cookies=cookies,
        csrf_token=csrf_token,
        session_id=session_id,
        extracted_at=time.time(),
    )

    # Save to cache
    save_tokens_to_cache(tokens)

    print()
    print("=" * 40)
    print("성공!")
    print("=" * 40)
    print()
    print(f"쿠키: {len(cookies)}개 추출됨")
    print(f"CSRF 토큰: {'있음' if csrf_token else '없음 (자동 추출될 예정)'}")
    print(f"세션 ID: {session_id or '자동 추출될 예정'}")
    print()
    print(f"토큰 저장 위치: {get_cache_path()}")
    print()
    print("다음 단계:")
    print()
    print("  1. AI 도구에 MCP 추가 (아직 하지 않은 경우):")
    print()
    print("     Claude Code:")
    print("       claude mcp add notebooklm-mcp -- notebooklm-mcp")
    print()
    print("     Gemini CLI:")
    print("       gemini mcp add notebooklm notebooklm-mcp")
    print()
    print("     또는 settings.json에 수동으로 추가:")
    print('       "notebooklm-mcp": { "command": "notebooklm-mcp" }')
    print()
    print("  2. AI 어시스턴트 재시작")
    print()
    print("  3. 테스트 질문: '내 NotebookLM 노트북 목록 보여줘'")
    print()

    # Close Chrome if we launched it - this unlocks the profile for headless auth
    if chrome_process:
        print("Chrome 닫는 중 (향후 헤드리스 인증을 위해 프로필 저장됨)...")
        try:
            chrome_process.terminate()
            chrome_process.wait(timeout=5)
        except Exception:
            try:
                chrome_process.kill()
            except Exception:
                pass
        print()

    return tokens


def run_file_cookie_entry(cookie_file: str | None = None) -> AuthTokens | None:
    """Read cookies from a file and save them.

    This is the recommended way to authenticate - users save their cookies
    to a text file to avoid terminal truncation issues.

    Args:
        cookie_file: Optional path to file. If not provided, shows instructions
                     and prompts for the path.
    """
    print("NotebookLM MCP - 쿠키 파일 가져오기")
    print("=" * 50)
    print()

    # If no file provided, show instructions and prompt for path
    if not cookie_file:
        print("쿠키를 추출하고 저장하려면 다음 단계를 따르세요:")
        print()
        print("  1. Chrome을 열고 이동: https://notebooklm.google.com")
        print("  2. 로그인이 되어 있는지 확인하세요.")
        print("  3. F12 (Mac은 Cmd+Option+I)를 눌러 개발자 도구를 엽니다.")
        print("  4. '네트워크(Network)' 탭을 클릭합니다.")
        print("  5. 필터 상자에 다음을 입력하세요: batchexecute")
        print("  6. 아무 노트북이나 클릭하여 요청을 발생시킵니다.")
        print("  7. 목록에서 'batchexecute' 요청을 클릭합니다.")
        print("  8. 오른쪽 패널에서 'Request Headers'를 찾습니다.")
        print("  9. 'cookie:'로 시작하는 줄을 찾습니다.")
        print(" 10. 쿠키 '값(Value)'을 우클릭하고 '값 복사(Copy value)'를 선택합니다.")
        print(" 11. 이 저장소의 'cookies.txt' 파일을 수정합니다 (또는 새 파일 생성).")
        print(" 12. 쿠키 문자열을 붙여넣고 저장합니다.")
        print()
        print("팁: 리포지토리 디렉터리에서 실행하는 경우 'cookies.txt'만 수정하고")
        print("     다음을 입력하세요: cookies.txt")
        print()
        print("-" * 50)
        print()

        try:
            cookie_file = input("쿠키 파일 경로를 입력하세요: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n취소됨.")
            return None

        if not cookie_file:
            print("오류: 파일 경로가 제공되지 않았습니다.")
            return None

        # Expand ~ to home directory
        cookie_file = str(Path(cookie_file).expanduser())

    print()
    print(f"쿠키 읽는 중: {cookie_file}")
    print()

    try:
        with open(cookie_file, "r") as f:
            cookie_string = f.read().strip()
    except FileNotFoundError:
        print(f"오류: 파일을 찾을 수 없음: {cookie_file}")
        return None
    except Exception as e:
        print(f"오류: 파일을 읽을 수 없음: {e}")
        return None

    # Strip comment lines (lines starting with #)
    lines = cookie_string.split("\n")
    cookie_lines = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    cookie_string = " ".join(cookie_lines)

    if not cookie_string:
        print("\n오류: 파일에서 쿠키 문자열을 찾을 수 없습니다.")
        print("쿠키를 붙여넣고 지침을 제거했는지 확인하세요.")
        return None

    print()
    print("쿠키 검증 중...")

    # Parse cookies from header format (key=value; key=value; ...)
    cookies = {}
    for cookie in cookie_string.split(";"):
        cookie = cookie.strip()
        if "=" in cookie:
            key, value = cookie.split("=", 1)
            cookies[key.strip()] = value.strip()

    if not cookies:
        print("\n오류: 입력에서 쿠키를 파싱할 수 없습니다.")
        print("헤더 이름이 아닌 쿠키 '값(Value)'을 복사했는지 확인하세요.")
        print()
        print("예상 형식: SID=xxx; HSID=xxx; SSID=xxx; ...")
        return None

    # Validate required cookies
    if not validate_cookies(cookies):
        print("\n경고: 일부 필수 쿠키가 누락되었습니다!")
        print(f"필수: {REQUIRED_COOKIES}")
        print(f"발견: {list(cookies.keys())}")
        print()
        print("계속 진행합니다...")

    # Create tokens object (CSRF and session ID will be auto-extracted later)
    tokens = AuthTokens(
        cookies=cookies,
        csrf_token="",  # Will be auto-extracted
        session_id="",  # Will be auto-extracted
        extracted_at=time.time(),
    )

    # Save to cache
    print()
    print("쿠키 저장 중...")
    save_tokens_to_cache(tokens)

    print()
    print("=" * 50)
    print("성공!")
    print("=" * 50)
    print()
    print(f"저장된 쿠키: {len(cookies)}개")
    print(f"캐시 위치: {get_cache_path()}")
    print()
    print("다음 단계:")
    print()
    print("  1. AI 도구에 MCP 추가 (아직 하지 않은 경우):")
    print()
    print("     Claude Code:")
    print("       claude mcp add notebooklm-mcp -- notebooklm-mcp")
    print()
    print("     Gemini CLI:")
    print("       gemini mcp add notebooklm notebooklm-mcp")
    print()
    print("     또는 settings.json에 수동으로 추가:")
    print('       "notebooklm-mcp": { "command": "notebooklm-mcp" }')
    print()
    print("  2. AI 어시스턴트 재시작")
    print()
    print("  3. 테스트 질문: '내 NotebookLM 노트북 목록 보여줘'")
    print()

    return tokens


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="NotebookLM MCP 인증 도구",
        epilog="""
이 도구는 NotebookLM MCP에서 사용할 인증 토큰을 Chrome에서 추출합니다.

두 가지 모드:

1. 파일 모드 (--file): 파일에서 쿠키 가져오기 (권장)
   - 쿠키 추출을 위한 단계별 지침을 보여줍니다.
   - 쿠키를 저장한 후 파일 경로를 입력받습니다.
   - Chrome 원격 디버깅이 필요하지 않습니다.

2. 자동 모드 (기본값): Chrome DevTools를 통한 자동 추출
   - 먼저 Chrome을 닫아야 합니다.
   - Chrome을 실행하고 자동으로 쿠키를 추출합니다.
   - 일부 시스템에서는 작동하지 않을 수 있습니다.

사용 예시:
  notebooklm-mcp-auth --file               # 안내가 포함된 파일 가져오기 (권장)
  notebooklm-mcp-auth --file ~/cookies.txt # 직접 파일 가져오기
  notebooklm-mcp-auth                      # 자동 모드 (Chrome 먼저 종료)

인증 후 다음 명령어로 MCP 서버를 시작하세요: notebooklm-mcp
        """
    )
    parser.add_argument(
        "--file",
        nargs="?",
        const="",  # When --file is used without argument, set to empty string
        metavar="PATH",
        help="파일에서 쿠키 가져오기 (권장). 경로가 없으면 지침을 표시합니다."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=CDP_DEFAULT_PORT,
        help=f"Chrome DevTools 포트 (기본값: {CDP_DEFAULT_PORT})"
    )
    parser.add_argument(
        "--show-tokens",
        action="store_true",
        help="캐시된 토큰 표시 (디버깅용)"
    )
    parser.add_argument(
        "--no-auto-launch",
        action="store_true",
        help="Chrome 자동 실행 안 함 (디버깅이 켜진 상태로 Chrome이 실행 중이어야 함)"
    )

    args = parser.parse_args()

    if args.show_tokens:
        cache_path = get_cache_path()
        if cache_path.exists():
            with open(cache_path) as f:
                data = json.load(f)
            print(json.dumps(data, indent=2))
        else:
            print("캐시된 토큰이 없습니다.")
        return 0

    try:
        if args.file is not None:  # --file was used (with or without path)
            # File-based cookie import
            tokens = run_file_cookie_entry(cookie_file=args.file if args.file else None)
        else:
            # Automatic extraction via Chrome DevTools
            tokens = run_auth_flow(args.port, auto_launch=not args.no_auto_launch)

        return 0 if tokens else 1
    except KeyboardInterrupt:
        print("\n취소됨.")
        return 1
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
