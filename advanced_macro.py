"""
인터파크 티켓팅 매크로 - 고급 기능 모듈
실제 API 리버스 엔지니어링 기반 구현
"""

import asyncio
import aiohttp
import json
import time
import re
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
import logging
import random
import string
from urllib.parse import urlencode, quote

logger = logging.getLogger('interpark_macro')


@dataclass
class CaptchaSolver:
    """캡차 해결 (외부 서비스 연동)"""
    api_key: Optional[str] = None
    service: str = "2captcha"  # 2captcha, anti-captcha, etc.
    
    async def solve(self, site_key: str, page_url: str) -> Optional[str]:
        """reCAPTCHA 해결"""
        if not self.api_key:
            return None
        
        # TODO: 캡차 서비스 API 연동
        # 2captcha API 예시:
        # 1. 캡차 제출
        # 2. 폴링으로 결과 대기
        # 3. 토큰 반환
        return None


@dataclass
class InterparkSession:
    """인터파크 세션 상태"""
    session: aiohttp.ClientSession
    cookies: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    token: Optional[str] = None
    queue_token: Optional[str] = None
    booking_token: Optional[str] = None
    last_activity: float = 0
    request_count: int = 0
    
    async def request(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        """요청 실행 (레이트 리밋 및 쿠키 관리)"""
        self.last_activity = time.time()
        self.request_count += 1
        
        # 쿠키 헤더 추가
        if self.cookies:
            cookie_str = '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
            kwargs.setdefault('headers', {})
            kwargs['headers']['Cookie'] = cookie_str
        
        async with self.session.request(method, url, **kwargs) as resp:
            # 응답 쿠키 저장
            for cookie in resp.cookies.values():
                self.cookies[cookie.key] = cookie.value
            return resp


class InterparkMacroAdvanced:
    """고급 인터파크 매크로 (실제 API 기반)"""
    
    # 인터파크 도메인
    DOMAINS = {
        'ticket': 'ticket.interpark.com',
        'nol': 'nol.interpark.com',
        'api': 'api.interpark.com',
        'booking': 'booking.interpark.com',
        'image': 'ticketimage.interpark.com',
    }
    
    def __init__(self):
        self.sessions: List[InterparkSession] = []
        self.accounts: List[Dict] = []
        self.proxies: List[str] = []
        self.captcha_solver: Optional[CaptchaSolver] = None
        
    def _make_headers(self, referer: Optional[str] = None) -> Dict[str, str]:
        """요청 헤더 생성"""
        headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Ch-Ua': '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"macOS"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        }
        if referer:
            headers['Referer'] = referer
        return headers
    
    async def _create_session(self, proxy: Optional[str] = None) -> InterparkSession:
        """새 세션 생성"""
        connector = aiohttp.TCPConnector(
            limit=50,
            limit_per_host=20,
            enable_cleanup_closed=True,
            force_close=False,
        )
        
        timeout = aiohttp.ClientTimeout(total=30, connect=5, sock_read=10)
        
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=self._make_headers(),
            trust_env=True,
        )
        
        return InterparkSession(session=session)
    
    async def _get(self, session: InterparkSession, url: str, **kwargs) -> aiohttp.ClientResponse:
        """GET 요청"""
        return await session.request('GET', url, proxy=proxy, **kwargs)
    
    async def _post(self, session: InterparkSession, url: str, **kwargs) -> aiohttp.ClientResponse:
        """POST 요청"""
        return await session.request('POST', url, proxy=proxy, **kwargs)
    
    async def login(self, session: InterparkSession, username: str, password: str) -> bool:
        """로그인 (실제 인터파크 로그인 흐름)"""
        try:
            # 1. 로그인 페이지 접속
            login_url = f"https://{self.DOMAINS['nol']}/login"
            resp = await self._get(session, login_url)
            text = await resp.text()
            
            # 2. CSRF 토큰 추출
            csrf_match = re.search(r'name="_csrf" value="([^"]+)"', text)
            csrf_token = csrf_match.group(1) if csrf_match else None
            
            # 3. 로그인 API 호출
            login_api = f"https://{self.DOMAINS['api']}/member/login"
            
            payload = {
                'memId': username,
                'memPwd': password,
                'deviceType': 'PC',
                'csrfToken': csrf_token,
            }
            
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': login_url,
            }
            
            resp = await self._post(session, login_api, data=urlencode(payload), headers=headers)
            result = await resp.json()
            
            if result.get('code') == '0000':
                session.token = result.get('data', {}).get('token')
                logger.info(f"로그인 성공: {username}")
                return True
            else:
                logger.error(f"로그인 실패: {result.get('message')}")
                return False
                
        except Exception as e:
            logger.error(f"로그인 예외: {e}")
            return False
    
    async def get_goods_info(self, session: InterparkSession, goods_code: str) -> Dict:
        """상품 정보 조회 (실제 인터파크 API)"""
        url = f"https://{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        params = {
            'GoodsCode': goods_code,
            'Tiki': '',
            'TikiToken': '',
            'MemberNo': '',
        }
        
        resp = await self._get(session, url, params=params)
        return await resp.json()
    
    async def get_play_date(self, session: InterparkSession, goods_code: str) -> List[Dict]:
        """공연 날짜 조회"""
        url = f"https://{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        params = {
            'Flag': 'PlaySeq',
            'GoodsCode': goods_code,
        }
        
        resp = await self._get(session, url, params=params)
        data = await resp.json()
        return data.get('data', {}).get('PlaySeqList', [])
    
    async def get_seat_info(self, session: InterparkSession, goods_code: str, play_seq: str) -> Dict:
        """좌석 정보 조회"""
        url = f"https://{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        params = {
            'Flag': 'SeatInfo',
            'GoodsCode': goods_code,
            'PlaySeq': play_seq,
        }
        
        resp = await self._get(session, url, params=params)
        return await resp.json()
    
    async def enter_booking(self, session: InterparkSession, goods_code: str, play_seq: str) -> Dict:
        """예매 페이지 진입 (대기열)"""
        url = f"https://{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        params = {
            'Flag': 'Booking',
            'GoodsCode': goods_code,
            'PlaySeq': play_seq,
        }
        
        resp = await self._get(session, url, params=params)
        return await resp.json()
    
    async def select_seat(self, session: InterparkSession, goods_code: str, play_seq: str, 
                          seat_block: str, seat_no: str) -> Dict:
        """좌석 선택"""
        url = f"https://{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        params = {
            'Flag': 'SeatSelect',
            'GoodsCode': goods_code,
            'PlaySeq': play_seq,
            'SeatBlock': seat_block,
            'SeatNo': seat_no,
        }
        
        resp = await self._get(session, url, params=params)
        return await resp.json()
    
    async def reserve(self, session: InterparkSession, goods_code: str, play_seq: str,
                      seats: List[Dict]) -> Dict:
        """예약 확정"""
        url = f"https://{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        
        seat_data = '|'.join([f"{s['block']}-{s['no']}" for s in seats])
        
        params = {
            'Flag': 'Reserve',
            'GoodsCode': goods_code,
            'PlaySeq': play_seq,
            'SeatData': seat_data,
        }
        
        resp = await self._get(session, url, params=params)
        return await resp.json()
    
    async def macro_booking(self, goods_code: str, target_date: str, 
                           target_time: Optional[str] = None,
                           seat_count: int = 1,
                           max_price: int = 999999999) -> List[Dict]:
        """매크로 예매 실행"""
        results = []
        
        # 모든 세션으로 동시 예매 시도
        async def try_book(session: InterparkSession) -> Dict:
            try:
                # 1. 공연 날짜 조회
                play_dates = await self.get_play_date(session, goods_code)
                
                target_play = None
                for play in play_dates:
                    if target_date in play.get('PlayDate', ''):
                        if not target_time or target_time in play.get('PlayTime', ''):
                            target_play = play
                            break
                
                if not target_play:
                    return {"status": "failed", "reason": "target_date_not_found"}
                
                play_seq = target_play['PlaySeq']
                
                # 2. 예매 진입 (대기열)
                booking_result = await self.enter_booking(session, goods_code, play_seq)
                
                if booking_result.get('code') != '0000':
                    return {"status": "failed", "reason": "booking_entry_failed"}
                
                # 3. 좌석 정보 조회
                seat_info = await self.get_seat_info(session, goods_code, play_seq)
                seats = seat_info.get('data', {}).get('SeatList', [])
                
                # 4. 사용 가능한 좌석 찾기
                available = [s for s in seats if s.get('SeatStatus') == '0']
                
                if len(available) < seat_count:
                    return {"status": "failed", "reason": "sold_out"}
                
                # 5. 좌석 선택 (랜덤 또는 우선순위)
                selected = available[:seat_count]
                
                # 6. 예약 확정
                reserve_result = await self.reserve(session, goods_code, play_seq, selected)
                
                if reserve_result.get('code') == '0000':
                    return {
                        "status": "success",
                        "booking_no": reserve_result.get('data', {}).get('BookingNo'),
                        "seats": selected
                    }
                else:
                    return {"status": "failed", "reason": "reserve_failed"}
                    
            except Exception as e:
                return {"status": "failed", "reason": "exception", "error": str(e)}
        
        # 동시 실행
        tasks = [try_book(s) for s in self.sessions]
        results = await asyncio.gather(*tasks)
        
        return list(results)
    
    async def close(self):
        """모든 세션 종료"""
        for session in self.sessions:
            await session.session.close()


class RealtimeMonitor:
    """실시간 모니터링 및 알림"""
    
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url
        self.stats = {
            'requests': 0,
            'success': 0,
            'failed': 0,
            'start_time': None,
        }
    
    def start(self):
        self.stats['start_time'] = time.time()
    
    def log_request(self, success: bool = False):
        self.stats['requests'] += 1
        if success:
            self.stats['success'] += 1
        else:
            self.stats['failed'] += 1
    
    def get_stats(self) -> Dict:
        elapsed = time.time() - self.stats['start_time'] if self.stats['start_time'] else 0
        rps = self.stats['requests'] / elapsed if elapsed > 0 else 0
        
        return {
            'requests': self.stats['requests'],
            'success': self.stats['success'],
            'failed': self.stats['failed'],
            'elapsed': round(elapsed, 2),
            'rps': round(rps, 2),
        }
    
    async def send_notification(self, message: str):
        """웹훅 알림 전송"""
        if not self.webhook_url:
            return
        
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(self.webhook_url, json={"text": message})
        except Exception as e:
            logger.error(f"알림 전송 실패: {e}")


# 실행 스크립트
async def run_macro():
    """매크로 실행"""
    macro = InterparkMacroAdvanced()
    monitor = RealtimeMonitor()
    
    # 세션 생성 (멀티세션)
    for i in range(3):  # 3개 세션
        session = await macro._create_session()
        macro.sessions.append(session)
    
    # 로그인
    accounts = [
        ("user1@example.com", "password1"),
        ("user2@example.com", "password2"),
        ("user3@example.com", "password3"),
    ]
    
    for i, (username, password) in enumerate(accounts):
        if i < len(macro.sessions):
            await macro.login(macro.sessions[i], username, password)
    
    # 예매 설정
    goods_code = "24012345"  # 공연 코드
    target_date = "2026-06-20"
    target_time = "19:30"
    
    monitor.start()
    
    # 예매 실행
    results = await macro.macro_booking(
        goods_code=goods_code,
        target_date=target_date,
        target_time=target_time,
        seat_count=2
    )
    
    # 결과 출력
    for i, result in enumerate(results):
        status = "✅" if result['status'] == 'success' else "❌"
        print(f"{status} 세션 {i+1}: {result}")
    
    # 통계
    stats = monitor.get_stats()
    print(f"\n통계: {stats}")
    
    await macro.close()


if __name__ == "__main__":
    asyncio.run(run_macro())
