#!/usr/bin/env python3
"""
인터파크 통합 매크로 - 캡처 + 예매 하나의 프로그램

실행 흐름:
1. 캡처 모드: Chrome DevTools Protocol로 API 자동 캡처
2. 사용자가 크롬에서 로그인 + 예매 페이지 진입
3. API 캡처되면 자동으로 매크로 모드 전환
4. 캡처된 API로 즉시 예매 실행

빌드: pyinstaller --onefile --console --name InterparkMacro interpark_unified.py
"""

import asyncio
import json
import time
import logging
import subprocess
import sys
import os
import threading
import queue
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('interpark_macro.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# ============== 데이터 클래스 ==============

@dataclass
class ProxyConfig:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    
    def to_url(self) -> str:
        if self.username and self.password:
            return f"http://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"http://{self.host}:{self.port}"


@dataclass
class AccountConfig:
    username: str
    password: str
    session_id: Optional[str] = None
    cookies: Dict[str, str] = field(default_factory=dict)


@dataclass
class SeatPreference:
    min_price: int = 0
    max_price: int = 999999999
    preferred_sections: List[str] = field(default_factory=list)
    preferred_floors: List[str] = field(default_factory=list)
    max_seats: int = 2
    priority: str = "price"


@dataclass
class DiscoveredAPI:
    endpoint: str
    url: str
    method: str = "GET"
    headers: Dict = field(default_factory=dict)
    sample_request: Optional[str] = None
    sample_response: Optional[str] = None
    timestamp: str = ""
    count: int = 1


# ============== Chrome DevTools 캡처 ==============

class ChromeDevToolsCapture:
    """Chrome DevTools Protocol로 네트워크 캡처"""
    
    def __init__(self, chrome_path: str = None, headless: bool = False):
        self.chrome_path = chrome_path or self._find_chrome()
        self.headless = headless
        self.debug_port = 9222
        self.ws_url = None
        self.chrome_proc = None
        self.discovered_apis: Dict[str, DiscoveredAPI] = {}
        self.api_queue = queue.Queue()  # 캡처된 API를 매크로에 전달
        self.is_capturing = False
        
    def _find_chrome(self) -> str:
        import shutil
        
        # macOS
        mac_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chrome.app/Contents/MacOS/Chrome",
        ]
        for p in mac_paths:
            if Path(p).exists():
                return p
        
        # Linux
        for name in ['google-chrome', 'chromium', 'chromium-browser', 'chrome']:
            path = shutil.which(name)
            if path:
                return path
        
        # Windows
        win_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for p in win_paths:
            if Path(p).exists():
                return p
        
        raise RuntimeError("Chrome을 찾을 수 없습니다.")
    
    def start_chrome(self):
        """디버깅 포트로 크롬 시작"""
        cmd = [
            self.chrome_path,
            f"--remote-debugging-port={self.debug_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-popup-blocking",
            "--disable-translate",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-hang-monitor",
            "--disable-prompt-on-repost",
            "--disable-renderer-backgrounding",
            "--force-color-profile=srgb",
            "--metrics-recording-only",
            "--safebrowsing-disable-auto-update",
            "--password-store=basic",
            "--use-mock-keychain",
        ]
        
        if self.headless:
            cmd.append("--headless")
        
        logger.info(f"Chrome 시작: {self.chrome_path}")
        self.chrome_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        time.sleep(3)
        self._connect_to_cdp()
    
    def _connect_to_cdp(self):
        import urllib.request
        
        url = f"http://localhost:{self.debug_port}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read())
                self.ws_url = data.get('webSocketDebuggerUrl')
                logger.info(f"CDP 연결 성공")
        except Exception as e:
            logger.error(f"CDP 연결 실패: {e}")
            raise
    
    async def capture(self, target_url: str, duration: int = 600):
        """네트워크 캡처 실행"""
        if not self.ws_url:
            raise RuntimeError("CDP에 연결되지 않음")
        
        self.is_capturing = True
        logger.info(f"캡처 시작: {target_url}")
        
        try:
            import websockets
        except ImportError:
            logger.error("websockets 모듈이 필요합니다: pip install websockets")
            return
        
        async with websockets.connect(self.ws_url) as ws:
            # Network 도메인 활성화
            await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
            await ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
            
            # 페이지 이동
            await ws.send(json.dumps({
                "id": 3,
                "method": "Page.navigate",
                "params": {"url": target_url}
            }))
            
            start_time = time.time()
            
            while self.is_capturing and (time.time() - start_time) < duration:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(message)
                    
                    if data.get('method') == 'Network.responseReceived':
                        params = data.get('params', {})
                        response = params.get('response', {})
                        url = response.get('url', '')
                        
                        if self._is_interpark_api(url):
                            await self._process_api(ws, params, url, response)
                    
                    elif data.get('method') == 'Runtime.consoleAPICalled':
                        params = data.get('params', {})
                        if params.get('type') == 'log':
                            args = params.get('args', [])
                            msgs = [a.get('value', '') for a in args if a.get('type') == 'string']
                            if msgs:
                                logger.info(f"Console: {' '.join(msgs)}")
                
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"캡처 오류: {e}")
            
            logger.info(f"캡처 완료: {len(self.discovered_apis)}개 API 발견")
    
    async def _process_api(self, ws, params, url, response):
        """API 응답 처리"""
        endpoint = url.split('?')[0].split('/')[-1] if '/api/' in url else url
        
        # 응답 본문 가져오기
        body = None
        request_id = params.get('requestId')
        if request_id:
            try:
                await ws.send(json.dumps({
                    "id": 200,
                    "method": "Network.getResponseBody",
                    "params": {"requestId": request_id}
                }))
                
                import websockets
                msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                msg_data = json.loads(msg)
                
                if 'result' in msg_data:
                    result = msg_data['result']
                    body = result.get('body', '')
                    if result.get('base64Encoded') and body:
                        import base64
                        body = base64.b64decode(body).decode('utf-8', errors='ignore')
            except:
                pass
        
        # API 정보 저장
        if endpoint not in self.discovered_apis:
            api = DiscoveredAPI(
                endpoint=endpoint,
                url=url,
                method="GET",  # TODO: request 메소드 추적
                headers=dict(response.get('headers', {})),
                sample_response=body[:1000] if body else None,
                timestamp=datetime.now().isoformat()
            )
            self.discovered_apis[endpoint] = api
            
            # 매크로 엔진에 전달
            self.api_queue.put(api)
            
            logger.info(f"🎯 새 API 발견: {endpoint}")
            if body:
                logger.info(f"   응답: {body[:200]}...")
        else:
            self.discovered_apis[endpoint].count += 1
    
    def _is_interpark_api(self, url: str) -> bool:
        domains = [
            'tickets.interpark.com/api',
            'api.interpark.com',
            'api.nol.interpark.com',
            'ticket.interpark.com',
            'nol.interpark.com/api',
        ]
        return any(d in url for d in domains)
    
    def stop(self):
        self.is_capturing = False
        if self.chrome_proc:
            logger.info("Chrome 종료 중...")
            self.chrome_proc.terminate()
            try:
                self.chrome_proc.wait(timeout=5)
            except:
                self.chrome_proc.kill()
    
    def get_api_queue(self) -> queue.Queue:
        return self.api_queue
    
    def get_discovered_apis(self) -> Dict[str, DiscoveredAPI]:
        return self.discovered_apis


# ============== 매크로 엔진 ==============

class MacroEngine:
    """예매 매크로 엔진"""
    
    def __init__(self, api_queue: queue.Queue, accounts: List[AccountConfig],
                 seat_pref: SeatPreference, goods_code: str):
        self.api_queue = api_queue
        self.accounts = accounts
        self.seat_pref = seat_pref
        self.goods_code = goods_code
        self.discovered_apis: Dict[str, DiscoveredAPI] = {}
        self.is_running = False
        self.results = []
        
    def update_apis(self):
        """캡처된 API 업데이트"""
        while not self.api_queue.empty():
            try:
                api = self.api_queue.get_nowait()
                self.discovered_apis[api.endpoint] = api
                logger.info(f"매크로 엔진에 API 추가: {api.endpoint}")
            except queue.Empty:
                break
    
    def has_required_apis(self) -> bool:
        """예매에 필요한 API가 모두 있는지 확인"""
        # TODO: 실제 필요한 API 패턴 확인
        required = ['playSchedule', 'seat', 'reserve', 'payment']
        discovered = [k.lower() for k in self.discovered_apis.keys()]
        
        # 일부라도 있으면 시작 (캡처 진행 중에도)
        has_any = any(
            any(r in d for d in discovered)
            for r in required
        )
        return has_any or len(self.discovered_apis) > 0
    
    async def run(self):
        """매크로 실행"""
        self.is_running = True
        logger.info("매크로 엔진 시작")
        
        try:
            while self.is_running:
                # 새 API 업데이트
                self.update_apis()
                
                # 필요한 API가 있으면 예매 시도
                if self.has_required_apis():
                    logger.info(f"{len(self.discovered_apis)}개 API로 예매 시도")
                    await self._attempt_booking()
                else:
                    logger.info("API 캡처 대기 중...")
                
                await asyncio.sleep(1)
        
        except Exception as e:
            logger.error(f"매크로 엔진 오류: {e}")
        finally:
            self.is_running = False
    
    async def _attempt_booking(self):
        """예매 시도 - 10회 재시도"""
        max_retries = 10
        
        for account in self.accounts:
            for attempt in range(max_retries):
                try:
                    logger.info(f"[{account.username}] 예매 시도 {attempt + 1}/{max_retries}")
                    
                    # TODO: 실제 예매 API 호출
                    # 캡처된 API를 사용해서:
                    # 1. 회차 조회
                    # 2. 좌석 조회
                    # 3. 좌석 선택 (208 → 207 → 209 → 206 → 210)
                    # 4. 결제
                    
                    # 성공 시 종료
                    # return True
                    
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"[{account.username}] 오류 (시도 {attempt + 1}): {e}")
                    
                    if attempt < max_retries - 1:
                        wait_time = 0.3 * (attempt + 1)
                        logger.info(f"[{account.username}] {wait_time}초 후 재시도...")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"[{account.username}] 10회 모두 실패")
    
    def stop(self):
        self.is_running = False


# ============== 통합 컨트롤러 ==============

class UnifiedMacro:
    """캡처 + 매크로 통합 컨트롤러"""
    
    def __init__(self):
        self.capture = None
        self.macro = None
        self.config = self._load_config()
        
    def _load_config(self) -> Dict:
        config_file = Path("macro_config.json")
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {
            'accounts': [],
            'goods_code': '26007886',
            'open_time': '2026-06-18T20:00:00+09:00',
            'seat_preference': {
                'min_price': 0,
                'max_price': 200000,
                'preferred_sections': [],
                'preferred_floors': ['1층'],
                'max_seats': 2,
                'priority': 'price'
            }
        }
    
    async def run(self):
        """통합 실행"""
        print("=" * 60)
        print("인터파크 통합 매크로")
        print("=" * 60)
        print()
        
        # 설정 확인
        if not self.config.get('accounts'):
            print("⚠️ 계정이 설정되지 않았습니다.")
            print("accounts.json 파일을 생성하세요:")
            print(json.dumps([{'username': '아이디', 'password': '비밀번호'}], indent=2, ensure_ascii=False))
            return
        
        goods_code = self.config.get('goods_code', '26007886')
        target_url = f"https://tickets.interpark.com/goods/{goods_code}"
        
        print(f"🎯 타겟 공연: {goods_code}")
        print(f"🌐 캡처 URL: {target_url}")
        print()
        
        # 계정 설정
        accounts = [
            AccountConfig(username=a['username'], password=a['password'])
            for a in self.config['accounts']
        ]
        
        seat_pref = SeatPreference(**self.config.get('seat_preference', {}))
        
        # 캡처 에이전트 시작
        self.capture = ChromeDevToolsCapture(headless=False)
        self.capture.start_chrome()
        
        # 매크로 엔진 시작 (캡처와 병렬)
        api_queue = self.capture.get_api_queue()
        self.macro = MacroEngine(api_queue, accounts, seat_pref, goods_code)
        
        print("✅ Chrome 시작 완료")
        print("✅ 매크로 엔진 시작")
        print()
        print("📋 다음 단계:")
        print("   1. Chrome에서 인터파크 로그인")
        print("   2. 예매 페이지까지 진행")
        print("   3. API가 캡처되면 매크로가 자동 실행")
        print()
        print("⏱️  캡처는 10분간 진행됩니다...")
        print()
        
        # 캡처와 매크로 병렬 실행
        capture_task = asyncio.create_task(
            self.capture.capture(target_url, duration=600)
        )
        macro_task = asyncio.create_task(
            self.macro.run()
        )
        
        try:
            await asyncio.gather(capture_task, macro_task)
        except asyncio.CancelledError:
            pass
        finally:
            self.stop()
    
    def stop(self):
        """종료"""
        print("\n🛑 종료 중...")
        if self.macro:
            self.macro.stop()
        if self.capture:
            self.capture.stop()
        
        # 결과 저장
        if self.capture:
            apis = self.capture.get_discovered_apis()
            with open('discovered_apis.json', 'w', encoding='utf-8') as f:
                json.dump({
                    k: {
                        'endpoint': v.endpoint,
                        'url': v.url,
                        'method': v.method,
                        'sample_response': v.sample_response,
                        'count': v.count,
                        'timestamp': v.timestamp
                    }
                    for k, v in apis.items()
                }, f, ensure_ascii=False, indent=2)
            
            print(f"\n📦 발견된 API: {len(apis)}개")
            for endpoint, api in apis.items():
                print(f"   • {endpoint} ({api.count}회)")


# ============== 메인 ==============

async def main():
    macro = UnifiedMacro()
    
    try:
        await macro.run()
    except KeyboardInterrupt:
        print("\n사용자가 중단했습니다.")
    finally:
        macro.stop()


if __name__ == "__main__":
    asyncio.run(main())
