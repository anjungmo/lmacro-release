"""
다클라 입력 스케줄러
- 마우스/키보드 입력을 큐에 넣고 순서대로 처리
- PRESS~RELEASE 구간은 원자적 (다른 클라 끼어들 수 없음)
- 각 클라별 독립 HuntWorker가 요청, 스케줄러가 실행
"""
import threading
import queue
import time
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL('user32')


class InputRequest:
    """입력 요청"""
    def __init__(self, client_id, hwnd, action, callback=None, priority=0):
        self.client_id = client_id  # 클라 식별자
        self.hwnd = hwnd            # 게임 창 핸들
        self.action = action        # callable: 실제 입력 동작
        self.callback = callback    # 완료 콜백
        self.priority = priority    # 0=보통, 1=전투, 2=긴급(HP)
        self.done = threading.Event()
        self.result = None

    def __lt__(self, other):
        # 우선순위 높은 것 먼저 (큰 숫자 = 높은 우선순위)
        return self.priority > other.priority


class InputScheduler:
    """글로벌 입력 스케줄러 — 모든 클라의 마우스/키보드 입력 관리"""

    def __init__(self, esp_cmd_fn):
        self._queue = queue.PriorityQueue()
        self._lock = threading.Lock()       # 현재 실행 중인 요청 보호
        self._active_client = None          # 현재 마우스를 쓰고 있는 클라
        self._press_held = False            # PRESS 홀드 중 (원자적 구간)
        self._esp_cmd = esp_cmd_fn          # ESP32/HWInput 명령 함수
        self._running = True
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()

    def submit(self, client_id, hwnd, action, priority=0, block=True, timeout=5.0):
        """입력 요청 제출. block=True면 완료까지 대기."""
        req = InputRequest(client_id, hwnd, action, priority=priority)
        self._queue.put(req)
        if block:
            req.done.wait(timeout=timeout)
            return req.result
        return req

    def submit_click(self, client_id, hwnd, x, y, priority=0):
        """간단한 클릭 요청"""
        def action():
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
            user32.SetCursorPos(x, y)
            time.sleep(0.03)
            self._esp_cmd("CLICK")
        return self.submit(client_id, hwnd, action, priority)

    def submit_press(self, client_id, hwnd, x, y, priority=1):
        """PRESS 요청 — 이후 RELEASE까지 이 클라가 마우스 독점"""
        def action():
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
            user32.SetCursorPos(x, y)
            time.sleep(0.03)
            self._esp_cmd("PRESS")
            self._press_held = True
            self._active_client = client_id
        return self.submit(client_id, hwnd, action, priority)

    def submit_release(self, client_id, hwnd, priority=1):
        """RELEASE 요청 — 마우스 독점 해제"""
        def action():
            self._esp_cmd("RELEASE")
            self._press_held = False
            self._active_client = None
        return self.submit(client_id, hwnd, action, priority)

    def submit_key(self, client_id, hwnd, key_cmd, priority=0):
        """키보드 입력 요청"""
        def action():
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
            self._esp_cmd(key_cmd)
        return self.submit(client_id, hwnd, action, priority)

    def _scheduler_loop(self):
        """메인 스케줄러 루프"""
        while self._running:
            try:
                req = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            with self._lock:
                # PRESS 홀드 중이면 같은 클라만 처리
                if self._press_held and req.client_id != self._active_client:
                    # 다른 클라 요청은 다시 큐에 넣기
                    self._queue.put(req)
                    time.sleep(0.05)
                    continue

                # 창 포커스 전환 (다른 클라로 바뀔 때만)
                try:
                    req.result = req.action()
                except Exception as e:
                    req.result = e

                req.done.set()

    def stop(self):
        self._running = False
        self._thread.join(timeout=2)


class HuntWorker:
    """단일 클라이언트 사냥 워커 — InputScheduler를 통해 입력"""

    def __init__(self, client_id, pid, hwnd, scheduler, pathfinder):
        self.id = client_id
        self.pid = pid
        self.hwnd = hwnd
        self.sched = scheduler
        self.pf = pathfinder
        self.running = False
        self._thread = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._hunt_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def click(self, x, y, priority=0):
        """스케줄러를 통한 클릭"""
        return self.sched.submit_click(self.id, self.hwnd, x, y, priority)

    def press(self, x, y):
        """스케줄러를 통한 PRESS (마우스 독점)"""
        return self.sched.submit_press(self.id, self.hwnd, x, y, priority=1)

    def release(self):
        """RELEASE (마우스 독점 해제)"""
        return self.sched.submit_release(self.id, self.hwnd, priority=1)

    def key(self, key_cmd, priority=0):
        """스케줄러를 통한 키 입력"""
        return self.sched.submit_key(self.id, self.hwnd, key_cmd, priority)

    def _hunt_loop(self):
        """사냥 루프 — autohunt.py의 hunt_loop를 재활용"""
        # TODO: 기존 hunt_loop 로직을 여기서 실행
        # self.click(), self.press(), self.release() 등으로 입력
        # self.pf로 메모리 읽기
        pass


# 사용 예시
if __name__ == '__main__':
    from hw_input import HWInput

    hw = HWInput()
    sched = InputScheduler(hw.send)

    print("스케줄러 시작. Ctrl+C로 종료")
    try:
        # 테스트: 2초 후 현재 위치에 클릭
        time.sleep(2)
        sched.submit_click("client1", 0, 500, 500)
        print("클릭 완료!")
    except KeyboardInterrupt:
        sched.stop()
