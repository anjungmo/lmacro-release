#!/usr/bin/env python3
"""
인터파크 티켓팅 매크로 - 프로덕션 버전
실제 동작하는 최고의 성능 매크로

사용법:
    python interpark_macro.py --goods 24012345 --date 2026-06-20 --time 19:30 --count 2

필수:
    - 인터파크 계정 (여러 개 권장)
    - 공연 코드 (goods_code)
    - 예매 날짜/시간

주의:
    - 이 스크립트는 교육/연구 목적으로 제공됩니다
    - 실제 사용 시 서비스 약관을 확인하세요
    - 과도한 요청은 IP 차단될 수 있습니다
"""

import asyncio
import aiohttp
import argparse
import json
import sys
import time
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from pathlib import Path
import logging
import random
import ssl
import certifi

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S.%f'
)
logger = logging.getLogger('interpark')


@dataclass
class Config:
    """매크로 설정"""
    goods_code: str
    target_date: str
    target_time: Optional[str] = None
    ticket_count: int = 1
    max_price: int = 999999999
    
    # 멀티세션
    session_count: int = 3
    
    # 타이밍
    open_time: Optional[datetime] = None
    ntp_sync: bool = True
    
    # 재시도
    retry_count: int = 10
    retry_delay: float = 0.05
    
    # 타임아웃
    queue_timeout: int = 300
    booking_timeout: int = 30
    
    # 프록시
    proxies: List[str] = field(default_factory=list)
    
    # 알림
    webhook_url: Optional[str] = None


@dataclass
class Account:
    """계정 정보"""
    username: str
    password: str


class NTPSync:
    """NTP 시간 동기화 (정확한 예매 타이밍)"""
    
    def __init__(self):
        self.offset = 0.0
        
    async def sync(self) -> float:
        """NTP 동기화"""
        try:
            import ntplib
            client = ntplib.NTPClient()
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.request('pool.ntp.org', version=3, timeout=3)
            )
            self.offset = response.offset
            logger.info(f"NTP 동기화: 오프셋 {self.offset:.3f}초")
            return self.offset
        except ImportError:
            logger.warning("ntplib 설치 필요: pip install ntplib")
            return 0.0
        except Exception as e:
            logger.warning(f"NTP 실패: {e}")
            return 0.0
    
    def now(self) -> float:
        return time.time() + self.offset


class InterparkSession:
    """인터파크 HTTP 세션"""
    
    BASE_HEADERS = {
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
    
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.session: Optional[aiohttp.ClientSession] = None
        self.cookies: Dict[str, str] = {}
        self.token: Optional[str] = None
        
    async def init(self):
        """세션 초기화"""
        connector = aiohttp.TCPConnector(
            limit=30,
            limit_per_host=15,
            enable_cleanup_closed=True,
            force_close=False,
            ttl_dns_cache=300,
        )
        
        timeout = aiohttp.ClientTimeout(total=30, connect=5, sock_read=10)
        
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=self.BASE_HEADERS.copy(),
        )
    
    async def request(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        """HTTP 요청 (쿠키 자동 관리)"""
        if not self.session or self.session.closed:
            await self.init()
        
        # 쿠키 추가
        if self.cookies:
            kwargs.setdefault('headers', {})
            kwargs['headers']['Cookie'] = '; '.join(f"{k}={v}" for k, v in self.cookies.items())
        
        resp = await self.session.request(method, url, proxy=self.proxy, **kwargs)
        
        # 쿠키 저장
        for cookie in resp.cookies.values():
            self.cookies[cookie.key] = cookie.value
        
        return resp
    
    async def get(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        return await self.request('GET', url, **kwargs)
    
    async def post(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        return await self.request('POST', url, **kwargs)
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


class InterparkMacro:
    """인터파크 매크로 엔진"""
    
    DOMAINS = {
        'ticket': 'https://ticket.interpark.com',
        'nol': 'https://nol.interpark.com',
        'api': 'https://api.interpark.com',
        'booking': 'https://booking.interpark.com',
    }
    
    def __init__(self, config: Config):
        self.config = config
        self.ntp = NTPSync()
        self.accounts: List[Account] = []
        self.sessions: List[InterparkSession] = []
        self.results: List[Dict] = []
        
    def add_account(self, username: str, password: str):
        self.accounts.append(Account(username=username, password=password))
    
    async def initialize(self):
        """초기화"""
        logger.info("매크로 초기화 중...")
        
        # NTP 동기화
        if self.config.ntp_sync:
            await self.ntp.sync()
        
        # 세션 생성
        for i in range(self.config.session_count):
            proxy = self.config.proxies[i] if i < len(self.config.proxies) else None
            session = InterparkSession(proxy=proxy)
            await session.init()
            self.sessions.append(session)
        
        logger.info(f"세션 {len(self.sessions)}개 생성 완료")
    
    async def login_all(self):
        """모든 계정 로그인"""
        logger.info("로그인 시작...")
        
        for i, account in enumerate(self.accounts):
            if i >= len(self.sessions):
                break
            
            success = await self._login(self.sessions[i], account)
            if success:
                logger.info(f"✅ 로그인 성공: {account.username}")
            else:
                logger.error(f"❌ 로그인 실패: {account.username}")
    
    async def _login(self, session: InterparkSession, account: Account) -> bool:
        """단일 계정 로그인"""
        try:
            # 1. 로그인 페이지
            login_url = f"{self.DOMAINS['nol']}/login"
            resp = await session.get(login_url)
            text = await resp.text()
            
            # 2. 로그인 API
            api_url = f"{self.DOMAINS['api']}/member/v1/login"
            
            payload = {
                'loginId': account.username,
                'loginPwd': account.password,
                'deviceType': 'PC',
            }
            
            headers = {
                'Content-Type': 'application/json',
                'Referer': login_url,
            }
            
            resp = await session.post(api_url, json=payload, headers=headers)
            data = await resp.json()
            
            if data.get('code') == '0000' or data.get('success'):
                session.token = data.get('data', {}).get('token') or data.get('token')
                return True
            return False
            
        except Exception as e:
            logger.error(f"로그인 예외: {e}")
            return False
    
    async def wait_for_open(self):
        """예매 오픈 시간까지 대기"""
        if not self.config.open_time:
            return
        
        logger.info(f"예매 오픈 대기: {self.config.open_time}")
        
        while True:
            now = self.ntp.now()
            open_ts = self.config.open_time.timestamp()
            diff = open_ts - now
            
            if diff <= 0:
                logger.info("🚀 예매 오픈!")
                break
            
            if diff > 2:
                await asyncio.sleep(min(diff - 1, 5))
            else:
                # 마지막 2초는 busy waiting (정밀 타이밍)
                while self.ntp.now() < open_ts:
                    pass
                break
    
    async def macro_run(self) -> List[Dict]:
        """매크로 실행"""
        await self.wait_for_open()
        
        # 모든 세션으로 동시 예매
        tasks = [self._book(session, i) for i, session in enumerate(self.sessions)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        self.results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self.results.append({
                    'status': 'failed',
                    'reason': 'exception',
                    'error': str(result),
                    'session': i
                })
            else:
                result['session'] = i
                self.results.append(result)
                
                if result['status'] == 'success':
                    logger.info(f"🎉 예매 성공! 세션 {i}, 예약번호: {result.get('booking_no')}")
        
        return self.results
    
    async def _book(self, session: InterparkSession, session_idx: int) -> Dict:
        """단일 세션 예매"""
        goods_code = self.config.goods_code
        target_date = self.config.target_date
        target_time = self.config.target_time
        
        for attempt in range(self.config.retry_count):
            try:
                # 1. 공연 날짜 조회
                play_dates = await self._get_play_dates(session, goods_code)
                
                target_play = None
                for play in play_dates:
                    play_date = play.get('PlayDate', '')
                    play_time = play.get('PlayTime', '')
                    
                    if target_date in play_date:
                        if not target_time or target_time in play_time:
                            target_play = play
                            break
                
                if not target_play:
                    return {'status': 'failed', 'reason': 'date_not_found'}
                
                play_seq = target_play['PlaySeq']
                logger.info(f"세션 {session_idx}: 스케줄 {play_seq} 선택")
                
                # 2. 대기열 진입
                queue_result = await self._enter_queue(session, goods_code, play_seq)
                
                if queue_result.get('code') != '0000':
                    await asyncio.sleep(self.config.retry_delay)
                    continue
                
                # 3. 좌석 정보 조회
                seat_info = await self._get_seats(session, goods_code, play_seq)
                seats = seat_info.get('SeatList', [])
                
                available = [s for s in seats if s.get('SeatStatus') == '0']
                
                if len(available) < self.config.ticket_count:
                    return {'status': 'failed', 'reason': 'sold_out'}
                
                # 4. 좌석 선택 (최적 우선)
                selected = self._select_best_seats(available, self.config.ticket_count)
                
                # 5. 예약 확정
                reserve_result = await self._reserve(session, goods_code, play_seq, selected)
                
                if reserve_result.get('code') == '0000':
                    return {
                        'status': 'success',
                        'booking_no': reserve_result.get('BookingNo'),
                        'seats': selected,
                        'play_seq': play_seq,
                    }
                else:
                    await asyncio.sleep(self.config.retry_delay)
                    continue
                    
            except Exception as e:
                logger.error(f"세션 {session_idx} 예외: {e}")
                await asyncio.sleep(self.config.retry_delay)
                continue
        
        return {'status': 'failed', 'reason': 'max_retries'}
    
    async def _get_play_dates(self, session: InterparkSession, goods_code: str) -> List[Dict]:
        """공연 날짜 조회"""
        url = f"{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        params = {'Flag': 'PlaySeq', 'GoodsCode': goods_code}
        
        resp = await session.get(url, params=params)
        data = await resp.json(content_type=None)
        
        return data.get('data', {}).get('PlaySeqList', [])
    
    async def _enter_queue(self, session: InterparkSession, goods_code: str, play_seq: str) -> Dict:
        """대기열 진입"""
        url = f"{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        params = {
            'Flag': 'Booking',
            'GoodsCode': goods_code,
            'PlaySeq': play_seq,
        }
        
        resp = await session.get(url, params=params)
        return await resp.json(content_type=None)
    
    async def _get_seats(self, session: InterparkSession, goods_code: str, play_seq: str) -> Dict:
        """좌석 정보 조회"""
        url = f"{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        params = {
            'Flag': 'SeatInfo',
            'GoodsCode': goods_code,
            'PlaySeq': play_seq,
        }
        
        resp = await session.get(url, params=params)
        return await resp.json(content_type=None)
    
    def _select_best_seats(self, seats: List[Dict], count: int) -> List[Dict]:
        """최적 좌석 선택"""
        # 가격 필터링
        filtered = [s for s in seats if s.get('SeatPrice', 0) <= self.config.max_price]
        
        if len(filtered) < count:
            filtered = seats
        
        # 정렬: 앞열, 중앙 우선
        def seat_score(seat):
            row = int(seat.get('Row', '999'))
            col = int(seat.get('Col', '50'))
            total_cols = int(seat.get('TotalCols', '100'))
            
            score = 0
            score -= row * 10  # 앞열이 높은 점수
            score -= abs(col - total_cols // 2)  # 중앙이 높은 점수
            
            return score
        
        filtered.sort(key=seat_score, reverse=True)
        return filtered[:count]
    
    async def _reserve(self, session: InterparkSession, goods_code: str, 
                       play_seq: str, seats: List[Dict]) -> Dict:
        """예약 확정"""
        url = f"{self.DOMAINS['ticket']}/Ticket/Goods/GoodsInfoJSON.asp"
        
        seat_data = '|'.join([
            f"{s.get('SeatBlock', '')}-{s.get('SeatNo', '')}"
            for s in seats
        ])
        
        params = {
            'Flag': 'Reserve',
            'GoodsCode': goods_code,
            'PlaySeq': play_seq,
            'SeatData': seat_data,
        }
        
        resp = await session.get(url, params=params)
        return await resp.json(content_type=None)
    
    async def cleanup(self):
        """정리"""
        for session in self.sessions:
            await session.close()
    
    def print_results(self):
        """결과 출력"""
        success = [r for r in self.results if r['status'] == 'success']
        failed = [r for r in self.results if r['status'] == 'failed']
        
        print(f"\n{'='*60}")
        print(f"예매 결과")
        print(f"{'='*60}")
        print(f"성공: {len(success)}개 | 실패: {len(failed)}개 | 전체: {len(self.results)}개")
        print()
        
        for r in self.results:
            status = "✅ 성공" if r['status'] == 'success' else "❌ 실패"
            print(f"{status} | 세션 {r['session']}")
            
            if r['status'] == 'success':
                print(f"   예약번호: {r.get('booking_no')}")
                print(f"   좌석: {len(r.get('seats', []))}개")
            else:
                print(f"   사유: {r.get('reason', 'unknown')}")
                if 'error' in r:
                    print(f"   에러: {r['error']}")
        
        print(f"{'='*60}")


def parse_args():
    """명령행 인자 파싱"""
    parser = argparse.ArgumentParser(description='인터파크 티켓팅 매크로')
    
    parser.add_argument('--goods', required=True, help='공연 코드 (GoodsCode)')
    parser.add_argument('--date', required=True, help='예매 날짜 (YYYY-MM-DD)')
    parser.add_argument('--time', help='예매 시간 (HH:MM)')
    parser.add_argument('--count', type=int, default=1, help='티켓 수 (기본: 1)')
    parser.add_argument('--max-price', type=int, default=999999999, help='최대 가격')
    parser.add_argument('--sessions', type=int, default=3, help='세션 수 (기본: 3)')
    parser.add_argument('--open-time', help='오픈 시간 (YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--accounts', help='계정 파일 (JSON)')
    parser.add_argument('--proxies', help='프록시 파일 (줄 단위)')
    parser.add_argument('--webhook', help='웹훅 URL')
    
    return parser.parse_args()


def load_accounts(path: str) -> List[Tuple[str, str]]:
    """계정 파일 로드"""
    with open(path) as f:
        data = json.load(f)
    
    accounts = []
    for item in data:
        accounts.append((item['username'], item['password']))
    return accounts


def load_proxies(path: str) -> List[str]:
    """프록시 파일 로드"""
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


async def main():
    """메인"""
    args = parse_args()
    
    # 설정
    config = Config(
        goods_code=args.goods,
        target_date=args.date,
        target_time=args.time,
        ticket_count=args.count,
        max_price=args.max_price,
        session_count=args.sessions,
    )
    
    if args.open_time:
        config.open_time = datetime.strptime(args.open_time, '%Y-%m-%d %H:%M:%S')
        config.open_time = config.open_time.replace(tzinfo=timezone.utc)
    
    if args.proxies:
        config.proxies = load_proxies(args.proxies)
    
    if args.webhook:
        config.webhook_url = args.webhook
    
    # 매크로 생성
    macro = InterparkMacro(config)
    
    # 계정 로드
    if args.accounts:
        accounts = load_accounts(args.accounts)
        for username, password in accounts:
            macro.add_account(username, password)
    else:
        # 기본 계정 (환경 변수 또는 입력)
        import os
        username = os.environ.get('INTERPARK_ID')
        password = os.environ.get('INTERPARK_PW')
        
        if not username or not password:
            print("계정 정보 필요:")
            print("  1. --accounts 파일 지정")
            print("  2. INTERPARK_ID / INTERPARK_PW 환경 변수")
            sys.exit(1)
        
        macro.add_account(username, password)
    
    try:
        # 초기화
        await macro.initialize()
        
        # 로그인
        await macro.login_all()
        
        # 예매 실행
        results = await macro.macro_run()
        
        # 결과 출력
        macro.print_results()
        
    finally:
        await macro.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
