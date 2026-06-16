#!/usr/bin/env python3
"""
인터파크 API 캡처 에이전트 - Chrome DevTools Protocol (CDP) 기반
크롬 브라우저를 헤드리스/헤드풀로 실행하고 네트워크 트래픽 자동 캡처
"""

import asyncio
import json
import time
import logging
import subprocess
import websockets
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Callable
import aiohttp

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class ChromeDevToolsCapture:
    """Chrome DevTools Protocol로 네트워크 캡처"""
    
    def __init__(self, chrome_path: str = None, headless: bool = False):
        self.chrome_path = chrome_path or self._find_chrome()
        self.headless = headless
        self.debug_port = 9222
        self.ws_url = None
        self.chrome_proc = None
        self.captured_requests = []
        self.capture_file = Path("captured_api.json")
        self._callbacks = []
        
    def _find_chrome(self) -> str:
        """시스템에서 크롬 실행 파일 찾기"""
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
        linux_names = ['google-chrome', 'chromium', 'chromium-browser', 'chrome']
        for name in linux_names:
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
        
        raise RuntimeError("Chrome을 찾을 수 없습니다. chrome_path를 지정하세요.")
    
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
            "--enable-logging",
            "--v=1",
        ]
        
        if self.headless:
            cmd.append("--headless")
        
        logger.info(f"Chrome 시작: {self.chrome_path} (port {self.debug_port})")
        self.chrome_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # CDP 엔드포인트 대기
        time.sleep(2)
        self._connect_to_cdp()
    
    def _connect_to_cdp(self):
        """CDP 엔드포인트 연결"""
        import urllib.request
        
        url = f"http://localhost:{self.debug_port}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read())
                self.ws_url = data.get('webSocketDebuggerUrl')
                logger.info(f"CDP 연결 성공: {self.ws_url}")
        except Exception as e:
            logger.error(f"CDP 연결 실패: {e}")
            raise
    
    async def capture_network(self, target_url: str, duration: int = 300):
        """네트워크 트래픽 캡처"""
        if not self.ws_url:
            raise RuntimeError("CDP에 연결되지 않음")
        
        logger.info(f"네트워크 캡처 시작: {target_url}")
        
        async with websockets.connect(self.ws_url) as ws:
            # Network 도메인 활성화
            await ws.send(json.dumps({
                "id": 1,
                "method": "Network.enable"
            }))
            
            # Fetch 도메인 활성화 (요청/응답 본문 캡처)
            await ws.send(json.dumps({
                "id": 2,
                "method": "Fetch.enable",
                "params": {
                    "patterns": [{"urlPattern": "*", "requestStage": "Response"}]
                }
            }))
            
            # Runtime 도메인 활성화 (console.log 캡처)
            await ws.send(json.dumps({
                "id": 3,
                "method": "Runtime.enable"
            }))
            
            # 페이지 이동
            await ws.send(json.dumps({
                "id": 4,
                "method": "Page.navigate",
                "params": {"url": target_url}
            }))
            
            # 캡처 시작 시간
            start_time = time.time()
            
            while time.time() - start_time < duration:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(message)
                    
                    # Network.responseReceived 처리
                    if data.get('method') == 'Network.responseReceived':
                        params = data.get('params', {})
                        response = params.get('response', {})
                        
                        url = response.get('url', '')
                        
                        # 인터파크 API만 필터링
                        if self._is_interpark_api(url):
                            api_data = {
                                'timestamp': datetime.now().isoformat(),
                                'url': url,
                                'status': response.get('status'),
                                'headers': response.get('headers', {}),
                                'mimeType': response.get('mimeType'),
                            }
                            
                            # 응답 본문 가져오기
                            request_id = params.get('requestId')
                            if request_id:
                                body = await self._get_response_body(ws, request_id)
                                if body:
                                    api_data['body'] = body
                            
                            self.captured_requests.append(api_data)
                            self._notify_callbacks(api_data)
                            
                            logger.info(f"API 캡처: {url}")
                            
                            # 파일에 즉시 저장
                            self._save_capture()
                    
                    # Fetch.requestPaused 처리 (Fetch API 사용 시)
                    elif data.get('method') == 'Fetch.requestPaused':
                        params = data.get('params', {})
                        request = params.get('request', {})
                        url = request.get('url', '')
                        
                        if self._is_interpark_api(url):
                            logger.info(f"Fetch 캡처: {url}")
                        
                        # 요청 계속 진행
                        await ws.send(json.dumps({
                            "id": 100 + int(time.time() * 1000) % 10000,
                            "method": "Fetch.continueRequest",
                            "params": {
                                "requestId": params.get('requestId')
                            }
                        }))
                    
                    # console.log 캡처
                    elif data.get('method') == 'Runtime.consoleAPICalled':
                        params = data.get('params', {})
                        if params.get('type') == 'log':
                            args = params.get('args', [])
                            messages = [arg.get('value', '') for arg in args if arg.get('type') == 'string']
                            if messages:
                                logger.info(f"Console: {' '.join(messages)}")
                
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"메시지 처리 오류: {e}")
            
            logger.info(f"캡처 완료: {len(self.captured_requests)}개 API 수집")
    
    async def _get_response_body(self, ws, request_id: str) -> Optional[str]:
        """응답 본문 가져오기"""
        try:
            await ws.send(json.dumps({
                "id": 200,
                "method": "Network.getResponseBody",
                "params": {"requestId": request_id}
            }))
            
            response = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(response)
            
            if 'result' in data:
                result = data['result']
                body = result.get('body', '')
                
                # base64 디코딩
                if result.get('base64Encoded') and body:
                    import base64
                    body = base64.b64decode(body).decode('utf-8', errors='ignore')
                
                return body
        except Exception as e:
            logger.warning(f"응답 본문 가져오기 실패: {e}")
        
        return None
    
    def _is_interpark_api(self, url: str) -> bool:
        """인터파크 API URL 필터링"""
        interpark_domains = [
            'tickets.interpark.com/api',
            'api.interpark.com',
            'api.nol.interpark.com',
            'ticket.interpark.com',
            'nol.interpark.com/api',
        ]
        return any(domain in url for domain in interpark_domains)
    
    def _notify_callbacks(self, api_data: Dict):
        """콜백 함수 호출"""
        for callback in self._callbacks:
            try:
                callback(api_data)
            except Exception as e:
                logger.error(f"콜백 오류: {e}")
    
    def add_callback(self, callback: Callable[[Dict], None]):
        """캡처 콜백 추가"""
        self._callbacks.append(callback)
    
    def _save_capture(self):
        """캡처 데이터 저장"""
        with open(self.capture_file, 'w', encoding='utf-8') as f:
            json.dump(self.captured_requests, f, ensure_ascii=False, indent=2)
    
    def stop(self):
        """크롬 종료"""
        if self.chrome_proc:
            logger.info("Chrome 종료 중...")
            self.chrome_proc.terminate()
            try:
                self.chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.chrome_proc.kill()
    
    def get_captured_apis(self) -> List[Dict]:
        """캡처된 API 목록 반환"""
        return self.captured_requests


class APICaptureMacro:
    """API 캡처 + 매크로 실행 통합"""
    
    def __init__(self):
        self.capture = ChromeDevToolsCapture(headless=False)
        self.discovered_apis = {}
        
    def on_api_captured(self, api_data: Dict):
        """API 캡처 콜백"""
        url = api_data.get('url', '')
        
        # API 패턴 분석
        if '/api/' in url:
            endpoint = url.split('/api/')[-1].split('?')[0]
            
            if endpoint not in self.discovered_apis:
                self.discovered_apis[endpoint] = {
                    'url': url,
                    'method': 'GET',  # TODO: 실제 메소드 추적
                    'first_seen': api_data.get('timestamp'),
                    'count': 0,
                    'sample_response': api_data.get('body', '')[:500] if api_data.get('body') else None
                }
                logger.info(f"새 API 발견: {endpoint}")
            
            self.discovered_apis[endpoint]['count'] += 1
            self.discovered_apis[endpoint]['last_seen'] = api_data.get('timestamp')
    
    async def run_capture_session(self, target_url: str, duration: int = 300):
        """캡처 세션 실행"""
        self.capture.add_callback(self.on_api_captured)
        self.capture.start_chrome()
        
        try:
            await self.capture.capture_network(target_url, duration)
        finally:
            self.capture.stop()
            
            # 결과 저장
            self._save_discovered_apis()
    
    def _save_discovered_apis(self):
        """발견된 API 저장"""
        output_file = Path("discovered_apis.json")
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(self.discovered_apis, f, ensure_ascii=False, indent=2)
        
        logger.info(f"발견된 API 저장 완료: {output_file}")
        
        # 요약 출력
        print("\n" + "="*60)
        print("발견된 API 목록")
        print("="*60)
        for endpoint, info in sorted(self.discovered_apis.items(), key=lambda x: x[1]['count'], reverse=True):
            print(f"  {endpoint}")
            print(f"    호출 횟수: {info['count']}")
            print(f"    URL: {info['url'][:80]}...")
            if info.get('sample_response'):
                print(f"    응답 샘플: {info['sample_response'][:100]}...")
            print()


async def main():
    """메인 실행"""
    macro = APICaptureMacro()
    
    # CORTIS 공연 페이지 캡처
    # 오픈 시간 직전에 실행하면 예매 API 캡처 가능
    target = "https://tickets.interpark.com/goods/26007886"
    
    print(f"API 캡처 시작: {target}")
    print("크롬이 열리면 로그인하고 예매 페이지까지 진행하세요.")
    print("캡처는 5분간 자동으로 진행됩니다.")
    print()
    
    await macro.run_capture_session(target, duration=300)
    
    print("\n캡처 완료!")
    print("discovered_apis.json 파일을 확인하세요.")


if __name__ == "__main__":
    asyncio.run(main())
