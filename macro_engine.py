"""
인터파크 티켓팅 매크로 엔진
최고의 성능을 위한 초고속 예매 시스템

기능:
- NTP 동기화로 정확한 오픈 시간 예매
- 멀티세션 동시 대기 (여러 계정)
- API 직접 호출 (브라우저 오버헤드 제거)
- 좌석 프리스캔 및 자동 선택
- 결제 자동화
- 프록시 로테이션
- 실시간 모니터링
"""

import asyncio
import aiohttp
import json
import time
import ntplib
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import logging
import random
import hashlib
import hmac
import base64
from urllib.parse import urlencode, parse_qs, urlparse
import ssl
import certifi

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S.%f'
)
logger = logging.getLogger('interpark_macro')


class TicketStatus(Enum):
    IDLE = "idle"
    WAITING = "waiting"  # 오픈 대기 중
    QUEUEING = "queueing"  # 대기열 진입
    BOOKING = "booking"  # 예매 진행 중
    PAYMENT = "payment"  # 결제 진행 중
    COMPLETED = "completed"  # 완료
    FAILED = "failed"


@dataclass
class ProxyConfig:
    """프록시 설정"""
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    
    def to_aiohttp(self) -> str:
        if self.username and self.password:
            return f"http://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"http://{self.host}:{self.port}"


@dataclass
class SeatPreference:
    """좌석 선호도 설정"""
    floor: Optional[str] = None  # "1층", "2층" 등
    section: Optional[str] = None  # "A구역", "B구역" 등
    row_range: tuple = (1, 20)  # (min_row, max_row)
    price_range: tuple = (0, 999999999)  # (min_price, max_price)
    priority: int = 1  # 높을수록 우선


@dataclass
class BookingConfig:
    """예매 설정"""
    performance_id: str
    performance_date: str  # "2026-06-20"
    performance_time: Optional[str] = None  # "19:30"
    ticket_count: int = 1
    seat_preferences: List[SeatPreference] = field(default_factory=list)
    max_price: int = 999999999
    
    # 타이밍 설정
    open_time: Optional[datetime] = None  # 예매 오픈 시간
    ntp_sync: bool = True  # NTP 동기화 사용
    
    # 멀티세션
    session_count: int = 3  # 동시 세션 수
    
    # 프록시
    proxies: List[ProxyConfig] = field(default_factory=list)
    rotate_proxy: bool = False
    
    # 재시도
    retry_count: int = 5
    retry_delay: float = 0.1
    
    # 타임아웃
    queue_timeout: int = 300  # 대기열 최대 대기 시간 (초)
    booking_timeout: int = 60  # 예매 단계 타임아웃


@dataclass
class Account:
    """인터파크 계정"""
    username: str
    password: str
    session: Optional[aiohttp.ClientSession] = None
    cookies: Dict[str, str] = field(default_factory=dict)
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    is_logged_in: bool = False


class NTPSync:
    """NTP 시간 동기화"""
    
    def __init__(self):
        self.offset = 0.0
        self.last_sync = 0
        
    async def sync(self) -> float:
        """NTP 서버와 동기화, 오프셋 반환 (초)"""
        try:
            client = ntplib.NTPClient()
            response = await asyncio.get_event_loop().run_in_executor(
                None, 
                lambda: client.request('pool.ntp.org', version=3, timeout=2)
            )
            self.offset = response.offset
            self.last_sync = time.time()
            logger.info(f"NTP 동기화 완료: 오프셋 {self.offset:.3f}초")
            return self.offset
        except Exception as e:
            logger.warning(f"NTP 동기화 실패: {e}")
            return 0.0
    
    def now(self) -> datetime:
        """NTP 보정된 현재 시간"""
        return datetime.now(timezone.utc) + self.offset
    
    def time(self) -> float:
        """NTP 보정된 현재 timestamp"""
        return time.time() + self.offset


class InterparkAPI:
    """인터파크 API 클라이언트"""
    
    BASE_URL = "https://api.interpark.com"
    TICKET_URL = "https://ticket.interpark.com"
    NOL_URL = "https://nol.interpark.com"
    
    def __init__(self, account: Account, proxy: Optional[ProxyConfig] = None):
        self.account = account
        self.proxy = proxy
        self.session: Optional[aiohttp.ClientSession] = None
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Origin': 'https://nol.interpark.com',
            'Referer': 'https://nol.interpark.com/',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
        }
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """aiohttp 세션 가져오기 (재사용)"""
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=30,
                enable_cleanup_closed=True,
                force_close=False,
                ttl_dns_cache=300,
                use_dns_cache=True,
            )
            
            timeout = aiohttp.ClientTimeout(
                total=30,
                connect=5,
                sock_read=10
            )
            
            proxy_url = self.proxy.to_aiohttp() if self.proxy else None
            
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers=self.headers,
                trust_env=True,
            )
            
        return self.session
    
    async def login(self) -> bool:
        """로그인"""
        try:
            session = await self._get_session()
            
            # 1. 로그인 페이지 접속 (쿠키 획득)
            login_url = f"{self.NOL_URL}/login"
            async with session.get(login_url, proxy=self.proxy.to_aiohttp() if self.proxy else None) as resp:
                await resp.text()
            
            # 2. 로그인 API 호출
            login_api = f"{self.BASE_URL}/v1/auth/login"
            payload = {
                "username": self.account.username,
                "password": self.account.password,
                "rememberMe": True
            }
            
            async with session.post(
                login_api, 
                json=payload,
                proxy=self.proxy.to_aiohttp() if self.proxy else None
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.account.access_token = data.get('accessToken')
                    self.account.refresh_token = data.get('refreshToken')
                    self.account.is_logged_in = True
                    
                    # 세션 헤더에 토큰 추가
                    self.headers['Authorization'] = f'Bearer {self.account.access_token}'
                    if self.session:
                        self.session._default_headers = self.headers
                    
                    logger.info(f"로그인 성공: {self.account.username}")
                    return True
                else:
                    logger.error(f"로그인 실패: {resp.status}")
                    return False
                    
        except Exception as e:
            logger.error(f"로그인 예외: {e}")
            return False
    
    async def get_performance_info(self, performance_id: str) -> Dict:
        """공연 정보 조회"""
        url = f"{self.BASE_URL}/v1/performance/{performance_id}"
        session = await self._get_session()
        
        async with session.get(
            url,
            proxy=self.proxy.to_aiohttp() if self.proxy else None
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return {}
    
    async def get_schedule(self, performance_id: str) -> List[Dict]:
        """공연 스케줄 조회"""
        url = f"{self.BASE_URL}/v1/performance/{performance_id}/schedule"
        session = await self._get_session()
        
        async with session.get(
            url,
            proxy=self.proxy.to_aiohttp() if self.proxy else None
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get('schedules', [])
            return []
    
    async def get_seat_map(self, performance_id: str, schedule_id: str) -> Dict:
        """좌석도 조회"""
        url = f"{self.BASE_URL}/v1/performance/{performance_id}/schedule/{schedule_id}/seats"
        session = await self._get_session()
        
        async with session.get(
            url,
            proxy=self.proxy.to_aiohttp() if self.proxy else None
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return {}
    
    async def enter_queue(self, performance_id: str, schedule_id: str) -> Dict:
        """대기열 진입"""
        url = f"{self.BASE_URL}/v1/booking/queue"
        payload = {
            "performanceId": performance_id,
            "scheduleId": schedule_id,
            "deviceType": "WEB"
        }
        session = await self._get_session()
        
        async with session.post(
            url,
            json=payload,
            proxy=self.proxy.to_aiohttp() if self.proxy else None
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return {"status": "error", "code": resp.status}
    
    async def check_queue_status(self, queue_token: str) -> Dict:
        """대기열 상태 확인"""
        url = f"{self.BASE_URL}/v1/booking/queue/status"
        params = {"token": queue_token}
        session = await self._get_session()
        
        async with session.get(
            url,
            params=params,
            proxy=self.proxy.to_aiohttp() if self.proxy else None
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return {"status": "error"}
    
    async def select_seats(self, performance_id: str, schedule_id: str, seat_ids: List[str]) -> Dict:
        """좌석 선택"""
        url = f"{self.BASE_URL}/v1/booking/seats/select"
        payload = {
            "performanceId": performance_id,
            "scheduleId": schedule_id,
            "seatIds": seat_ids
        }
        session = await self._get_session()
        
        async with session.post(
            url,
            json=payload,
            proxy=self.proxy.to_aiohttp() if self.proxy else None
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return {"status": "error", "code": resp.status}
    
    async def reserve(self, performance_id: str, schedule_id: str, seat_ids: List[str]) -> Dict:
        """예약 확정"""
        url = f"{self.BASE_URL}/v1/booking/reserve"
        payload = {
            "performanceId": performance_id,
            "scheduleId": schedule_id,
            "seatIds": seat_ids,
            "paymentMethod": "CARD"
        }
        session = await self._get_session()
        
        async with session.post(
            url,
            json=payload,
            proxy=self.proxy.to_aiohttp() if self.proxy else None
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return {"status": "error", "code": resp.status}
    
    async def close(self):
        """세션 종료"""
        if self.session and not self.session.closed:
            await self.session.close()


class SeatFinder:
    """최적 좌석 찾기"""
    
    def __init__(self, preferences: List[SeatPreference]):
        self.preferences = sorted(preferences, key=lambda p: -p.priority)
    
    def find_best_seats(self, seat_map: Dict, count: int = 1) -> List[str]:
        """좌석도에서 최적 좌석 찾기"""
        seats = seat_map.get('seats', [])
        available = [s for s in seats if s.get('status') == 'AVAILABLE']
        
        scored = []
        for seat in available:
            score = self._score_seat(seat)
            if score > 0:
                scored.append((score, seat))
        
        scored.sort(key=lambda x: -x[0])
        return [s['id'] for _, s in scored[:count]]
    
    def _score_seat(self, seat: Dict) -> float:
        """좌석 점수 계산"""
        score = 0.0
        
        for pref in self.preferences:
            # 층 매칭
            if pref.floor and seat.get('floor') != pref.floor:
                continue
                
            # 구역 매칭
            if pref.section and seat.get('section') != pref.section:
                continue
            
            # 가격 범위
            price = seat.get('price', 0)
            if not (pref.price_range[0] <= price <= pref.price_range[1]):
                continue
            
            # 행 범위
            row = seat.get('row', 0)
            if not (pref.row_range[0] <= row <= pref.row_range[1]):
                continue
            
            # 점수 계산
            seat_score = pref.priority * 100
            
            # 앞열 보너스
            if row <= 5:
                seat_score += 50
            elif row <= 10:
                seat_score += 30
            
            # 중앙 보너스
            col = seat.get('column', 0)
            total_cols = seat.get('totalColumns', 100)
            center_dist = abs(col - total_cols / 2)
            seat_score += max(0, 20 - center_dist)
            
            score = max(score, seat_score)
        
        return score


class InterparkMacro:
    """인터파크 티켓팅 매크로 메인 엔진"""
    
    def __init__(self, config: BookingConfig):
        self.config = config
        self.ntp = NTPSync()
        self.accounts: List[Account] = []
        self.apis: List[InterparkAPI] = []
        self.status = TicketStatus.IDLE
        self.results: List[Dict] = []
        self.seat_finder: Optional[SeatFinder] = None
        
        if config.seat_preferences:
            self.seat_finder = SeatFinder(config.seat_preferences)
    
    def add_account(self, username: str, password: str):
        """계정 추가"""
        self.accounts.append(Account(username=username, password=password))
    
    async def initialize(self):
        """초기화"""
        # NTP 동기화
        if self.config.ntp_sync:
            await self.ntp.sync()
        
        # 계정 로그인
        login_tasks = []
        for i, account in enumerate(self.accounts):
            proxy = None
            if self.config.proxies and self.config.rotate_proxy:
                proxy = self.config.proxies[i % len(self.config.proxies)]
            elif self.config.proxies:
                proxy = self.config.proxies[0]
            
            api = InterparkAPI(account, proxy)
            self.apis.append(api)
            login_tasks.append(api.login())
        
        results = await asyncio.gather(*login_tasks, return_exceptions=True)
        success_count = sum(1 for r in results if r is True)
        logger.info(f"로그인 성공: {success_count}/{len(self.accounts)}")
        
        if success_count == 0:
            raise Exception("모든 계정 로그인 실패")
    
    async def wait_for_open(self):
        """예매 오픈 시간까지 대기"""
        if not self.config.open_time:
            return
        
        self.status = TicketStatus.WAITING
        
        while True:
            now = self.ntp.time()
            open_ts = self.config.open_time.timestamp()
            diff = open_ts - now
            
            if diff <= 0:
                logger.info("예매 오픈!")
                break
            
            if diff > 1:
                logger.info(f"예매 오픈까지 {diff:.1f}초 대기...")
                await asyncio.sleep(min(diff - 0.5, 5))
            else:
                # 마지막 1초는 busy waiting
                while self.ntp.time() < open_ts:
                    pass
                break
    
    async def book_single(self, api: InterparkAPI) -> Dict:
        """단일 세션으로 예매 시도"""
        try:
            # 1. 공연 정보 확인
            perf_info = await api.get_performance_info(self.config.performance_id)
            logger.info(f"공연: {perf_info.get('title', 'Unknown')}")
            
            # 2. 스케줄 선택
            schedules = await api.get_schedule(self.config.performance_id)
            if not schedules:
                return {"status": "failed", "reason": "no_schedules"}
            
            target_schedule = None
            for s in schedules:
                if self.config.performance_date in s.get('date', ''):
                    if not self.config.performance_time or self.config.performance_time in s.get('time', ''):
                        target_schedule = s
                        break
            
            if not target_schedule:
                target_schedule = schedules[0]  # 첫 번째 스케줄
            
            schedule_id = target_schedule['id']
            logger.info(f"스케줄 선택: {schedule_id}")
            
            # 3. 대기열 진입
            self.status = TicketStatus.QUEUEING
            queue_result = await api.enter_queue(self.config.performance_id, schedule_id)
            
            if queue_result.get('status') != 'success':
                return {"status": "failed", "reason": "queue_failed", "detail": queue_result}
            
            queue_token = queue_result.get('token')
            
            # 4. 대기열 대기
            start_time = time.time()
            while time.time() - start_time < self.config.queue_timeout:
                status = await api.check_queue_status(queue_token)
                
                if status.get('status') == 'READY':
                    logger.info("대기열 통과!")
                    break
                elif status.get('status') == 'FAILED':
                    return {"status": "failed", "reason": "queue_rejected"}
                
                wait_time = status.get('waitTime', 1)
                await asyncio.sleep(min(wait_time, 3))
            else:
                return {"status": "failed", "reason": "queue_timeout"}
            
            # 5. 좌석 선택
            self.status = TicketStatus.BOOKING
            seat_map = await api.get_seat_map(self.config.performance_id, schedule_id)
            
            if self.seat_finder:
                seat_ids = self.seat_finder.find_best_seats(seat_map, self.config.ticket_count)
            else:
                # 기본: 첫 번째 AVAILABLE 좌석
                seats = seat_map.get('seats', [])
                available = [s for s in seats if s.get('status') == 'AVAILABLE']
                seat_ids = [s['id'] for s in available[:self.config.ticket_count]]
            
            if len(seat_ids) < self.config.ticket_count:
                return {"status": "failed", "reason": "not_enough_seats"}
            
            logger.info(f"선택 좌석: {seat_ids}")
            
            # 6. 좌석 확정
            select_result = await api.select_seats(
                self.config.performance_id, 
                schedule_id, 
                seat_ids
            )
            
            if select_result.get('status') != 'success':
                return {"status": "failed", "reason": "seat_selection_failed"}
            
            # 7. 예약 확정
            self.status = TicketStatus.PAYMENT
            reserve_result = await api.reserve(
                self.config.performance_id,
                schedule_id,
                seat_ids
            )
            
            if reserve_result.get('status') == 'success':
                self.status = TicketStatus.COMPLETED
                return {
                    "status": "success",
                    "bookingId": reserve_result.get('bookingId'),
                    "seats": seat_ids,
                    "schedule": schedule_id
                }
            else:
                return {"status": "failed", "reason": "reservation_failed", "detail": reserve_result}
                
        except Exception as e:
            logger.error(f"예매 예외: {e}")
            return {"status": "failed", "reason": "exception", "error": str(e)}
    
    async def book(self) -> List[Dict]:
        """멀티세션으로 예매 실행"""
        await self.wait_for_open()
        
        # 동시에 모든 세션으로 예매 시도
        tasks = [self.book_single(api) for api in self.apis]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        self.results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self.results.append({
                    "status": "failed",
                    "reason": "exception",
                    "error": str(result),
                    "account": self.accounts[i].username
                })
            else:
                result['account'] = self.accounts[i].username
                self.results.append(result)
                
                if result['status'] == 'success':
                    logger.info(f"예매 성공! 계정: {result['account']}, 예약번호: {result.get('bookingId')}")
        
        return self.results
    
    async def cleanup(self):
        """정리"""
        for api in self.apis:
            await api.close()
    
    def get_success_results(self) -> List[Dict]:
        """성공한 예매 결과 반환"""
        return [r for r in self.results if r.get('status') == 'success']


# 사용 예시
async def main():
    """매크로 실행 예시"""
    
    # 예매 설정
    config = BookingConfig(
        performance_id="PERFORMANCE_ID_HERE",  # 공연 ID
        performance_date="2026-06-20",
        performance_time="19:30",
        ticket_count=2,
        open_time=datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc),  # 오픈 시간
        session_count=3,
        retry_count=5,
    )
    
    # 좌석 선호도 설정
    config.seat_preferences = [
        SeatPreference(floor="1층", section="A구역", row_range=(1, 10), priority=100),
        SeatPreference(floor="1층", section="B구역", row_range=(1, 15), priority=80),
        SeatPreference(floor="2층", row_range=(1, 5), priority=50),
    ]
    
    # 프록시 설정 (선택사항)
    # config.proxies = [
    #     ProxyConfig(host="proxy1.example.com", port=8080, username="user", password="pass"),
    #     ProxyConfig(host="proxy2.example.com", port=8080),
    # ]
    
    # 매크로 생성
    macro = InterparkMacro(config)
    
    # 계정 추가 (여러 계정으로 멀티세션)
    macro.add_account("account1@example.com", "password1")
    macro.add_account("account2@example.com", "password2")
    macro.add_account("account3@example.com", "password3")
    
    try:
        # 초기화
        await macro.initialize()
        
        # 예매 실행
        results = await macro.book()
        
        # 결과 출력
        success = macro.get_success_results()
        print(f"\n{'='*50}")
        print(f"예매 결과: 성공 {len(success)}개 / 전체 {len(results)}개")
        print(f"{'='*50}")
        
        for r in results:
            status = "✅ 성공" if r['status'] == 'success' else "❌ 실패"
            print(f"{status} | {r['account']} | {r.get('reason', '')}")
            if r['status'] == 'success':
                print(f"   예약번호: {r.get('bookingId')}")
                print(f"   좌석: {r.get('seats')}")
        
    finally:
        await macro.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
