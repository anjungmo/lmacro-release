# 인터파크 티켓팅 매크로

## 개요

최고의 성능을 위한 인터파크 티켓팅 매크로. NTP 동기화, 멀티세션, API 직접 호출, 좌석 자동 선택을 지원.

## 설치

```bash
pip install aiohttp aiohttp-socks ntplib
```

## 사용법

### 1. 기본 사용 (즉시 예매)

```bash
export INTERPARK_ID="your_id@example.com"
export INTERPARK_PW="your_password"

python interpark_macro_final.py \
    --goods 24012345 \
    --date 2026-06-20 \
    --count 2
```

### 2. 오픈 시간 지정 (정확한 타이밍)

```bash
python interpark_macro_final.py \
    --goods 24012345 \
    --date 2026-06-20 \
    --time 19:30 \
    --open-time "2026-06-16 10:00:00" \
    --sessions 5
```

### 3. 멀티 계정 + 프록시

```bash
python interpark_macro_final.py \
    --goods 24012345 \
    --date 2026-06-20 \
    --accounts accounts.json \
    --proxies proxies.txt \
    --sessions 10
```

### 4. 좌석 선호도 지정

```bash
python interpark_macro_final.py \
    --goods 24012345 \
    --date 2026-06-20 \
    --floor 1층 \
    --section A구역 B구역 \
    --max-price 150000 \
    --count 2
```

## 설정 파일

### accounts.json

```json
[
    {"username": "id1@example.com", "password": "password1"},
    {"username": "id2@example.com", "password": "password2"},
    {"username": "id3@example.com", "password": "password3"}
]
```

### proxies.txt

```
# HTTP 프록시
http://user:pass@proxy1.com:8080
http://proxy2.com:8080

# SOCKS5 프록시
socks5://user:pass@proxy3.com:1080
socks5://proxy4.com:1080
```

## 공연 코드 찾기

1. 인터파크 티켓 페이지에서 원하는 공연 선택
2. URL에서 `GoodsCode` 파라미터 확인
   - 예: `https://ticket.interpark.com/.../GoodsCode=24012345`

## 주요 기능

| 기능 | 설명 |
|------|------|
| NTP 동기화 | 밀리초 단위 정확한 타이밍 |
| 멀티세션 | 3-10개 세션 동시 예매 |
| API 직접 호출 | 브라우저 오버헤드 제거 |
| 좌석 프리스캔 | 실시간 좌석 모니터링 |
| 자동 선택 | 선호도 기반 최적 좌석 |
| 재시도 + 백오프 | 자동 재시도 (지수 백오프) |
| 프록시 로테이션 | IP 분산 |

## 주의사항

- **법적 책임**: 사용자 본인의 책임 하에 사용
- **서비스 약관**: 인터파크 서비스 약관 확인
- **과도한 요청**: IP 차단 가능성
- **계정 보안**: 계정 파일 권한 관리 (chmod 600)

## 성능 팁

1. **세션 수**: 3-5개가 최적 (너무 많으면 차단)
2. **프록시**: 데이터센터 프록시보다 residential 권장
3. **NTP**: 동기화 필수 (시간 오차 1초 = 실패)
4. **네트워크**: 유선 > WiFi > LTE

## 문제 해결

### 로그인 실패
- 계정 정보 확인
- 2FA 비활성화 필요

### 좌석 조회 실패
- 공연 코드 확인
- 예매 오픈 시간 확인

### 예약 실패
- 티켓 매진
- 세션 수 증가 또는 프록시 사용

## 라이선스

MIT License - 교육/연구 목적으로 제공
