"""
리니지 하드웨어 매크로 v4 — 다중클라 동시성 큐
- 전역 InputScheduler (PriorityQueue) → LcHide IOCTL 직렬화
- 다중클라 탭 (각 클라 독립)
- scan_dll.dll 로 현재 좌표 읽기
- 웨이포인트 (한줄당 x,y)
- 대사(채팅) 리스트
- 무제한 / 1회
- n~m초 랜덤 딜레이
- 좌클릭 / 우클릭 / 더블클릭
"""
import tkinter as tk
from tkinter import ttk, filedialog
import ctypes, ctypes.wintypes as wt
import psutil, threading, time, random, os, sys, queue

# stdout 버퍼링 해제
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

user32 = ctypes.WinDLL('user32')
kernel32 = ctypes.WinDLL('kernel32')
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

from hw_input import HWInput
print("[OK] hw_input 로드")
hw = HWInput()

# scan_dll.dll
_dll_path = os.path.join(BASE, 'scan_dll.dll')
print(f"[INFO] scan_dll 경로: {_dll_path}")
print(f"[INFO] 파일존재: {os.path.exists(_dll_path)}")
_scan = ctypes.CDLL(_dll_path)
print("[OK] scan_dll 로드")
_scan.scan_init.restype = ctypes.c_int
_scan.scan_init.argtypes = [ctypes.c_int, ctypes.c_int]
_scan.scan_read_player.restype = ctypes.c_int
_scan.scan_read_player.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
try:
    _scan.scan_read_loc.restype = ctypes.c_int
    _scan.scan_read_loc.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
    print("[OK] scan_read_loc 바인딩")
except Exception as _e:
    print(f"[WARN] scan_read_loc 없음: {_e}")
print("[INFO] scan_init 호출...")
_init_result = _scan.scan_init(0, 0)
print(f"[INFO] scan_init 결과: {_init_result}")
if _init_result > 0:
    _scan.scan_get_player_addr.restype = ctypes.c_int64
    _scan.scan_get_exe_base.restype = ctypes.c_int
    _paddr = _scan.scan_get_player_addr()
    _exebase = _scan.scan_get_exe_base()
    print(f"[DEBUG] playerAddr=0x{_paddr:X} exeBase=0x{_exebase:X}")
    x, y = ctypes.c_int(), ctypes.c_int()
    rr = _scan.scan_read_player(ctypes.byref(x), ctypes.byref(y))
    print(f"[DEBUG] read_player result={rr} x={x.value} y={y.value}")

# PathFinder (A*)
try:
    from core import astar_path
    print("[OK] astar_path 모듈 로드")
except Exception as _e:
    print(f"[WARN] astar_path 로드 실패: {_e}")
    astar_path = None

def do_rescan(max_hp, max_mp=0):
    """Re-scan with exact maxHP for precise matching"""
    print(f"[INFO] 재스캔: maxHP={max_hp} maxMP={max_mp}")
    _init_result = _scan.scan_init(max_hp, max_mp or 0)
    print(f"[INFO] 재스캔 결과: {_init_result}")
    if _init_result > 0:
        x, y = ctypes.c_int(), ctypes.c_int()
        rr = _scan.scan_read_player(ctypes.byref(x), ctypes.byref(y))
        print(f"[DEBUG] read_player result={rr} x={x.value} y={y.value}")
    return _init_result


# ═══════════════════════════════════════════
#  전역 입력 스케줄러 — 단일 IOCTL 직렬화
# ═══════════════════════════════════════════
class InputRequest:
    __slots__ = ('cid', 'action', 'done', 'result', 'prio')
    def __init__(self, cid, action, prio=0):
        self.cid = cid
        self.action = action
        self.prio = prio
        self.done = threading.Event()
        self.result = None
    def __lt__(self, o):
        return self.prio > o.prio

class InputScheduler:
    def __init__(self):
        self._q = queue.PriorityQueue()
        self._lock = threading.Lock()
        self._active = None
        self._pressing = False
        self._running = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def submit(self, cid, action, prio=0, block=True, timeout=5.0):
        req = InputRequest(cid, action, prio)
        self._q.put(req)
        if block:
            req.done.wait(timeout=timeout)
        return req

    def _loop(self):
        while self._running:
            try:
                req = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            with self._lock:
                if self._pressing and req.cid != self._active:
                    self._q.put(req)
                    time.sleep(0.02)
                    continue
                try:
                    req.result = req.action()
                except Exception as e:
                    req.result = e
                req.done.set()

    def stop(self):
        self._running = False

sched = InputScheduler()


# ═══════════════════════════════════════════
#  유틸
# ═══════════════════════════════════════════
def find_pids():
    procs = [(p.info['pid'], p.info['name']) for p in psutil.process_iter(['pid','name'])
            if p.info['name'] and p.info['name'].lower() in ('lc.exe','sv.exe')]
    if not procs:
        all_procs = [(p.info['pid'], p.info['name']) for p in psutil.process_iter(['pid','name'])
                     if p.info['name'] and ('lin' in p.info['name'].lower() or 'lc' in p.info['name'].lower() or 'sv' in p.info['name'].lower())]
        if all_procs:
            print(f"[WARN] lc.exe/sv.exe 없음, 유사프로세스: {all_procs[:5]}")
        else:
            print("[WARN] 리니지 프로세스 전혀 없음")
    return [p[0] for p in procs]

def find_hwnd(pid):
    r=[None]
    def cb(h,_):
        p=wt.DWORD()
        user32.GetWindowThreadProcessId(h,ctypes.byref(p))
        if p.value==pid and user32.IsWindowVisible(h): r[0]=h; return False
        return True
    user32.EnumWindows(WNDENUMPROC(cb),0)
    return r[0]

_loc_cache = {'x': None, 'y': None, 't': 0, 'loc_sent': 0}
_LOC_TTL = 3.0       # 캐시 유효시간(초)
_LOC_INTERVAL = 5.0  # /loc 재전송 최소 간격

def _send_loc_async():
    """백그라운드에서 /loc 입력 (논블로킹)"""
    now = time.time()
    if now - _loc_cache['loc_sent'] < _LOC_INTERVAL:
        return  # 너무 자주 치지 않음
    _loc_cache['loc_sent'] = now
    import threading
    def _do():
        hw.key(0x0D); time.sleep(0.2)
        hw.type_text("/loc"); time.sleep(0.08)
        hw.key(0x0D)
    threading.Thread(target=_do, daemon=True).start()

def read_pos():
    """좌표 읽기 — 캐시 우선, 만료시 메모리 스캔 → /loc 입력"""
    now = time.time()
    # 캐시 유효하면 즉시 반환
    if _loc_cache['x'] is not None and now - _loc_cache['t'] < _LOC_TTL:
        return _loc_cache['x'], _loc_cache['y']
    # 메모리에서 바로 읽기 시도 (빠름)
    x,y=ctypes.c_int(),ctypes.c_int()
    if hasattr(_scan, 'scan_read_loc'):
        r = _scan.scan_read_loc(ctypes.byref(x),ctypes.byref(y))
        if r:
            _loc_cache.update(x=x.value, y=y.value, t=now)
            return x.value, y.value
    r = _scan.scan_read_player(ctypes.byref(x),ctypes.byref(y))
    if r:
        _loc_cache.update(x=x.value, y=y.value, t=now)
        return x.value, y.value
    # 실패 → /loc 비동기 전송 후 캐시 반환
    _send_loc_async()
    if _loc_cache['x'] is not None:
        return _loc_cache['x'], _loc_cache['y']
    return None, None

def tile2scr(hwnd, px,py, tx,ty):
    cr=wt.RECT(); user32.GetClientRect(hwnd,ctypes.byref(cr))
    cw,ch=cr.right,cr.bottom
    pt=wt.POINT(0,0); user32.ClientToScreen(hwnd,ctypes.byref(pt))
    a=24.0*cw/800; b=12.0*ch/600
    cx=pt.x+cw//2; cy=pt.y+ch//2-int(ch*90/900)
    dx,dy=tx-px,ty-py
    sx,sy = cx+int(a*(dx+dy)), cy+int(b*(dy-dx))
    print(f"[TILE] cw={cw} ch={ch} pt=({pt.x},{pt.y}) center=({cx},{cy}) dx={dx} dy={dy} → scr({sx},{sy})", flush=True)
    return sx, sy

def _fg(hwnd):
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.03)

def _mv(x, y):
    """마우스 이동 — SetCursorPos로 화면 좌표 이동 후 드라이버로 클릭"""
    user32.SetCursorPos(x, y)
    time.sleep(0.03)

# ═══════════════════════════════════════════
#  색상 (Catppuccin Mocha)
# ═══════════════════════════════════════════
BG="#1e1e2e"; BG2="#181825"; BG3="#313244"; FG="#cdd6f4"; FG2="#a6adc8"
GREEN="#a6e3a1"; RED="#f38ba8"; BLUE="#89b4fa"; YELLOW="#f9e2af"
ACCENT="#cba6f7"; TEAL="#94e2d5"; PEACH="#fab387"


# ═══════════════════════════════════════════
#  클라이언트 탭
# ═══════════════════════════════════════════
class ClientTab:
    def __init__(self, nb, pid, slot):
        self.pid = pid
        self.hwnd = find_hwnd(pid)
        self.slot = slot
        self.cid = f"c{slot}"
        self.running = False
        self.pf = None  # PathFinder 인스턴스 (지연 초기화)
        self.frame = tk.Frame(nb, bg=BG)
        nb.add(self.frame, text=f" 클라{slot} (PID {pid}) ")
        self._build()

    # ── 위젯 헬퍼 ──
    def _lbl(self, p, t, **kw):
        return tk.Label(p, text=t, bg=BG2, fg=FG2, font=("맑은 고딕",9), **kw)
    def _btn(self, p, t, cmd, color=GREEN, **kw):
        return tk.Button(p, text=t, command=cmd, bg=color, fg="black",
                         font=("맑은 고딕",9,"bold"), relief='flat', cursor='hand2',
                         activebackground=color, **kw)
    def _ent(self, p, w=6):
        return tk.Entry(p, width=w, bg=BG3, fg=FG, insertbackground=FG,
                        font=("Consolas",10), relief='flat')

    # ── UI 구성 ──
    def _build(self):
        f = self.frame

        # ── 캐릭터 정보 ──
        info = tk.LabelFrame(f, text=" 캐릭터 ", bg=BG2, fg=ACCENT,
                             font=("맑은 고딕",10,"bold"), bd=1, relief='groove')
        info.pack(fill='x', padx=8, pady=(8,4))
        r = tk.Frame(info, bg=BG2); r.pack(fill='x', padx=4, pady=4)
        self.pos_var = tk.StringVar(value="좌표: (?,?)")
        tk.Label(r, textvariable=self.pos_var, bg=BG2, fg=GREEN,
                 font=("Consolas",11,"bold")).pack(side='left')
        self._btn(r, "📍 좌표가져오기", self._get_pos, TEAL).pack(side='left', padx=12)

        # ── 웨이포인트 ──
        wp = tk.LabelFrame(f, text=" 웨이포인트 (한줄당 x,y) ", bg=BG2, fg=ACCENT,
                           font=("맑은 고딕",10,"bold"), bd=1, relief='groove')
        wp.pack(fill='both', expand=True, padx=8, pady=4)
        self.wp_text = tk.Text(wp, height=5, bg=BG3, fg=FG, font=("Consolas",10),
                               insertbackground=FG, relief='flat', selectbackground="#585b70")
        self.wp_text.pack(fill='both', expand=True, padx=4, pady=2)
        wr = tk.Frame(wp, bg=BG2); wr.pack(fill='x', padx=4, pady=2)
        self._lbl(wr, "x,y:").pack(side='left')
        self.wp_entry = self._ent(wr, 12); self.wp_entry.pack(side='left', padx=4)
        self._btn(wr, "추가", self._add_wp).pack(side='left', padx=2)
        self._btn(wr, "📍 현재위치", self._add_cur, ACCENT).pack(side='left', padx=2)
        self._btn(wr, "💾 저장", self._save, BLUE).pack(side='right', padx=2)
        self._btn(wr, "📂 불러오기", self._load, BLUE).pack(side='right', padx=2)

        # ── 대사 리스트 ──
        cf = tk.LabelFrame(f, text=" 대사 리스트 ", bg=BG2, fg=ACCENT,
                           font=("맑은 고딕",10,"bold"), bd=1, relief='groove')
        cf.pack(fill='both', expand=True, padx=8, pady=4)
        lr = tk.Frame(cf, bg=BG2); lr.pack(fill='both', expand=True, padx=4, pady=2)
        self.chat_lb = tk.Listbox(lr, height=4, bg=BG3, fg=FG, font=("맑은 고딕",10),
                                  selectbackground="#585b70", relief='flat')
        self.chat_lb.pack(side='left', fill='both', expand=True)
        sb = tk.Scrollbar(lr, command=self.chat_lb.yview); sb.pack(side='right', fill='y')
        self.chat_lb.config(yscrollcommand=sb.set)
        cr = tk.Frame(cf, bg=BG2); cr.pack(fill='x', padx=4, pady=2)
        self._lbl(cr, "대사:").pack(side='left')
        self.chat_entry = tk.Entry(cr, width=22, bg=BG3, fg=FG, insertbackground=FG,
                                   font=("맑은 고딕",10), relief='flat')
        self.chat_entry.pack(side='left', padx=4)
        self._btn(cr, "추가", self._add_chat).pack(side='left', padx=2)
        self._btn(cr, "삭제", self._del_chat, RED).pack(side='left', padx=2)
        self._btn(cr, "▲", self._chat_up, BLUE).pack(side='left', padx=1)
        self._btn(cr, "▼", self._chat_dn, BLUE).pack(side='left', padx=1)
        self._lbl(cr, " 타이밍:").pack(side='left', padx=(8,0))
        self.chat_delay = self._ent(cr, 4); self.chat_delay.insert(0, "3"); self.chat_delay.pack(side='left', padx=2)
        self._lbl(cr, "초").pack(side='left')

        # ── 명령 추가 ──
        cmd = tk.LabelFrame(f, text=" 명령 추가 ", bg=BG2, fg=ACCENT,
                            font=("맑은 고딕",10,"bold"), bd=1, relief='groove')
        cmd.pack(fill='x', padx=8, pady=4)
        cr2 = tk.Frame(cmd, bg=BG2); cr2.pack(fill='x', padx=4, pady=2)
        self.cmd_type = ttk.Combobox(cr2, values=["좌클릭","우클릭","더블클릭"],
                                      state="readonly", width=8)
        self.cmd_type.set("좌클릭"); self.cmd_type.pack(side='left')
        self._lbl(cr2, " X:").pack(side='left')
        self.cmd_x = self._ent(cr2, 6); self.cmd_x.pack(side='left')
        self._lbl(cr2, " Y:").pack(side='left')
        self.cmd_y = self._ent(cr2, 6); self.cmd_y.pack(side='left')
        self._btn(cr2, "추가", self._add_cmd).pack(side='left', padx=4)

        # ── 실행 설정 ──
        ctrl = tk.LabelFrame(f, text=" 실행 ", bg=BG2, fg=ACCENT,
                             font=("맑은 고딕",10,"bold"), bd=1, relief='groove')
        ctrl.pack(fill='x', padx=8, pady=4)
        r1 = tk.Frame(ctrl, bg=BG2); r1.pack(fill='x', padx=4, pady=2)
        self.loop_var = tk.StringVar(value="무제한")
        tk.Radiobutton(r1, text="무제한", variable=self.loop_var, value="무제한",
                       bg=BG2, fg=FG, selectcolor=BG3, activebackground=BG2,
                       font=("맑은 고딕",9)).pack(side='left')
        tk.Radiobutton(r1, text="1회", variable=self.loop_var, value="1회",
                       bg=BG2, fg=FG, selectcolor=BG3, activebackground=BG2,
                       font=("맑은 고딕",9)).pack(side='left')
        self._lbl(r1, "  목적지후:").pack(side='left')
        self.d_min = self._ent(r1, 3); self.d_min.insert(0, "1"); self.d_min.pack(side='left', padx=2)
        tk.Label(r1, text="~", bg=BG2, fg=FG2, font=("Consolas",10)).pack(side='left')
        self.d_max = self._ent(r1, 3); self.d_max.insert(0, "3"); self.d_max.pack(side='left', padx=2)
        self._lbl(r1, "초").pack(side='left')

        r1b = tk.Frame(ctrl, bg=BG2); r1b.pack(fill='x', padx=4, pady=2)
        self._lbl(r1b, "대사 간격:").pack(side='left')
        self.c_min = self._ent(r1b, 3); self.c_min.insert(0, "3"); self.c_min.pack(side='left', padx=2)
        tk.Label(r1b, text="~", bg=BG2, fg=FG2, font=("Consolas",10)).pack(side='left')
        self.c_max = self._ent(r1b, 3); self.c_max.insert(0, "5"); self.c_max.pack(side='left', padx=2)
        self._lbl(r1b, "초").pack(side='left')

        r2 = tk.Frame(ctrl, bg=BG2); r2.pack(fill='x', padx=4, pady=4)
        self._btn(r2, "▶ 시작", self._start, GREEN).pack(side='left', padx=4)
        self._btn(r2, "■ 정지", self._stop, RED).pack(side='left', padx=4)
        self.status_var = tk.StringVar(value="대기")
        tk.Label(r2, textvariable=self.status_var, bg=BG2, fg=YELLOW,
                 font=("Consolas",10)).pack(side='left', padx=8)

    # ── 스케줄러 래핑 입력 ──
    def _do_click(self, sx, sy, mode="좌클릭"):
        """스케줄러 큐에 클릭 요청"""
        sw = user32.GetSystemMetrics(0)  # SM_CXSCREEN
        sh = user32.GetSystemMetrics(1)  # SM_CYSCREEN
        # 화면 밖이면 가장자리로 클램핑
        clamped = False
        if sx < 0: sx = 0; clamped = True
        if sy < 0: sy = 0; clamped = True
        if sx >= sw: sx = sw - 1; clamped = True
        if sy >= sh: sy = sh - 1; clamped = True
        if clamped:
            print(f"[CLAMP] 화면 밖 → ({sx},{sy}) screen={sw}x{sh})", flush=True)
        def action():
            print(f"[CLICK] ({sx},{sy}) {mode}")
            _mv(sx, sy)
            time.sleep(0.05)
            # 검증: 이동 후 실제 커서 위치
            ax, ay = ctypes.c_long(), ctypes.c_long()
            user32.GetCursorPos(ctypes.byref(ax), ctypes.byref(ay))
            print(f"[POS] 이동후 커서=({ax.value},{ay.value}) 목표=({sx},{sy})", flush=True)
            if mode == "좌클릭":     hw.click()
            elif mode == "우클릭":   hw.right_click()
            elif mode == "더블클릭": hw.double_click()
        sched.submit(self.cid, action)

    def _do_type(self, text):
        """스케줄러 큐에 타이핑 요청 — 드라이버 키보드"""
        def action():
            print(f"[TYPE] 시작: '{text}'")
            # 채팅창 열기 (Enter)
            hw.key(0x0D)
            time.sleep(0.3)
            has_korean = any(ord(ch) >= 0x80 for ch in text)
            if has_korean:
                # 클립보드에 텍스트 복사
                data = text.encode('utf-16-le') + b'\x00\x00'
                user32.OpenClipboard(0)
                user32.EmptyClipboard()
                kernel32.GlobalAlloc.restype = ctypes.c_void_p
                kernel32.GlobalLock.restype = ctypes.c_void_p
                h = kernel32.GlobalAlloc(0x0002, len(data))
                p = kernel32.GlobalLock(h)
                ctypes.memmove(p, data, len(data))
                kernel32.GlobalUnlock(h)
                user32.SetClipboardData(13, h)  # CF_UNICODETEXT
                user32.CloseClipboard()
                time.sleep(0.05)
                # Ctrl+V — 드라이버 경로 (SendInput은 게임에서 무시됨)
                hw._drv_kbd(0x1D, up=False)  # Ctrl down (scancode)
                time.sleep(0.05)
                hw._drv_kbd(0x2F, up=False)  # V down (scancode)
                time.sleep(0.05)
                hw._drv_kbd(0x2F, up=True)   # V up
                time.sleep(0.03)
                hw._drv_kbd(0x1D, up=True)   # Ctrl up
                print(f"[CHAT] 클립보드+드라이버 Ctrl+V: {text}")
            else:
                hw.type_text(text)
                print(f"[CHAT] 드라이버 타이핑: {text}")
            time.sleep(0.1)
            hw.key(0x0D)  # Enter - 전송
            print(f"[TYPE] 전송완료")
        sched.submit(self.cid, action)

    # ── UI 액션 ──
    def _get_pos(self):
        x, y = read_pos()
        self.pos_var.set(f"좌표: ({x},{y})" if x is not None else "좌표: 읽기실패")

    def _add_wp(self):
        t = self.wp_entry.get().strip()
        if t: self.wp_text.insert(tk.END, t+"\n"); self.wp_entry.delete(0, tk.END)

    def _add_cur(self):
        do_rescan(0, 0)
        time.sleep(0.3)
        x, y = read_pos()
        paddr = 0
        try: paddr = _scan.scan_get_player_addr()
        except: pass
        # 후보 전체 덤프
        try:
            _scan.scan_get_cand_count.restype = ctypes.c_int
            _scan.scan_get_cand_addr.restype = ctypes.c_int64
            _scan.scan_get_cand_addr.argtypes = [ctypes.c_int]
            _scan.scan_get_cand_hp.restype = ctypes.c_int
            _scan.scan_get_cand_hp.argtypes = [ctypes.c_int]
            ncand = _scan.scan_get_cand_count()
            print(f"[CAND] {ncand} candidates (current playerAddr=0x{paddr:X}):", flush=True)
            pid = _scan.scan_get_pid()
            for i in range(ncand):
                addr = _scan.scan_get_cand_addr(i)
                mhp = _scan.scan_get_cand_hp(i)
                print(f"  [{i}] addr=0x{addr:X} mHP={mhp}", flush=True)
        except Exception as e:
            print(f"[CAND] err: {e}", flush=True)
        if x is not None:
            self.wp_text.insert(tk.END, f"{x},{y}\n")
            self.pos_var.set(f"좌표: ({x},{y})")

    def _add_chat(self):
        t = self.chat_entry.get().strip()
        if t: self.chat_lb.insert(tk.END, t); self.chat_entry.delete(0, tk.END)

    def _del_chat(self):
        s = self.chat_lb.curselection()
        if s: self.chat_lb.delete(s[0])

    def _chat_up(self):
        s = self.chat_lb.curselection()
        if not s or s[0]==0: return
        i=s[0]; v=self.chat_lb.get(i)
        self.chat_lb.delete(i); self.chat_lb.insert(i-1,v); self.chat_lb.select_set(i-1)

    def _chat_dn(self):
        s = self.chat_lb.curselection()
        if not s or s[0]>=self.chat_lb.size()-1: return
        i=s[0]; v=self.chat_lb.get(i)
        self.chat_lb.delete(i); self.chat_lb.insert(i+1,v); self.chat_lb.select_set(i+1)

    def _save(self):
        import json
        data = {
            "waypoints": self.wp_text.get("1.0", tk.END).strip(),
            "chats": [self.chat_lb.get(i) for i in range(self.chat_lb.size())],
            "delay_wp": [self.d_min.get(), self.d_max.get()],
            "delay_chat": [self.c_min.get(), self.c_max.get()],
        }
        path = filedialog.asksaveasfilename(defaultextension=".json",
                    filetypes=[("JSON","*.json")], title="프리셋 저장")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    def _load(self):
        import json
        path = filedialog.askopenfilename(filetypes=[("JSON","*.json")], title="프리셋 불러오기")
        if not path: return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "waypoints" in data:
            self.wp_text.delete("1.0", tk.END)
            self.wp_text.insert("1.0", data["waypoints"])
        if "chats" in data:
            self.chat_lb.delete(0, tk.END)
            for c in data["chats"]:
                self.chat_lb.insert(tk.END, c)
        if "delay_wp" in data:
            self.d_min.delete(0, tk.END); self.d_min.insert(0, data["delay_wp"][0])
            self.d_max.delete(0, tk.END); self.d_max.insert(0, data["delay_wp"][1])
        if "delay_chat" in data:
            self.c_min.delete(0, tk.END); self.c_min.insert(0, data["delay_chat"][0])
            self.c_max.delete(0, tk.END); self.c_max.insert(0, data["delay_chat"][1])

    def _add_cmd(self):
        t = self.cmd_type.get()
        x = self.cmd_x.get().strip(); y = self.cmd_y.get().strip()
        if not x or not y: return
        self.wp_text.insert(tk.END, f"{x},{y} # {t}\n")
        self.cmd_x.delete(0, tk.END); self.cmd_y.delete(0, tk.END)

    def _start(self):
        if self.running: return
        self.running = True; self.status_var.set("실행중")
        print(f"[START] 스레드 시작 hwnd={self.hwnd}", flush=True)
        threading.Thread(target=self._run, daemon=True).start()

    def _stop(self):
        self.running = False; self.status_var.set("정지")

    def _sleep(self, sec):
        for _ in range(int(sec*10)):
            if not self.running: return False
            time.sleep(0.1)
        return True

    def _run(self):
        print("[RUN] 진입", flush=True)
        try:
            # PathFinder 지연 초기화
            if astar_path and self.pf is None:
                try:
                    self.pf = astar_path.PathFinder(target_pid=self.pid)
                    print(f"[OK] PathFinder 초기화 완료 pid={self.pid}", flush=True)
                except Exception as e:
                    print(f"[WARN] PathFinder 초기화 실패: {e}, 직선이동 fallback", flush=True)
                    self.pf = None

            inf = self.loop_var.get() == "무제한"
            dmin = float(self.d_min.get() or 1)
            dmax = float(self.d_max.get() or 3)
            cmin = float(self.c_min.get() or 3)
            cmax = float(self.c_max.get() or 5)

            while self.running:
                # 1) 웨이포인트 이동 (A*)
                wp = self.wp_text.get("1.0", tk.END).strip()
                print(f"[RUN] wp={repr(wp[:100])} hwnd={self.hwnd}", flush=True)
                if wp:
                    for line in wp.split("\n"):
                        if not self.running: break
                        line = line.strip()
                        if not line: continue
                        parts = line.split("#")
                        coord = parts[0].replace(" ","").split(",")
                        mode = parts[1].strip() if len(parts) > 1 else "좌클릭"
                        if len(coord) < 2: continue
                        try: tx, ty = int(coord[0]), int(coord[1])
                        except: continue

                        # 목적지까지 이동
                        if self.pf:
                            self._run_astar_walk(tx, ty, mode)
                        else:
                            # fallback: 기존 직선 스텝 이동
                            self._run_direct_walk(tx, ty, mode)

                        # 목적지 도착 후 딜레이
                        if self.running:
                            self._sleep(random.uniform(dmin, dmax))

                # 2) 대사 전송
                for i in range(self.chat_lb.size()):
                    if not self.running: break
                    msg = self.chat_lb.get(i)
                    self._do_type(msg)
                    if not self._sleep(random.uniform(cmin, cmax)): break

                if not inf: break

            self.running = False
            self.status_var.set("완료")
        except Exception as e:
            print(f"[RUN ERROR] {e}", flush=True)
            import traceback; traceback.print_exc()
            self.running = False
            self.status_var.set("에러")

    def _run_astar_walk(self, tx, ty, mode="좌클릭"):
        """A* 길찾기로 웨이포인트까지 이동"""
        WALK_TIMEOUT = 60  # 최대 60초
        start = time.monotonic()
        while self.running:
            if time.monotonic() - start > WALK_TIMEOUT:
                print(f"[A*] 타임아웃 ({tx},{ty})", flush=True)
                break
            px, py = read_pos()
            if px is None:
                time.sleep(1); continue
            dist = max(abs(tx - px), abs(ty - py))
            if dist <= 2:
                print(f"[A*] 도착 ({px},{py})", flush=True)
                break
            # A* 경로 탐색
            try:
                path, waypoints, elapsed = self.pf.find_path(px, py, tx, ty)
            except Exception as e:
                print(f"[A*] find_path 에러: {e}", flush=True)
                path, waypoints = [], []
            if not path or len(path) < 2:
                print(f"[A*] 경로 없음 직선이동", flush=True)
                self._run_direct_walk(tx, ty, mode)
                return
            # 웨이포인트 따라 걷기
            for wi in range(1, len(waypoints)):
                if not self.running: return
                wx, wy = waypoints[wi]
                # 화면 좌표로 변환 후 클릭
                sx, sy = tile2scr(self.hwnd, px, py, wx, wy)
                print(f"[A*] wp({wx},{wy}) → scr({sx},{sy})", flush=True)
                self._do_click(sx, sy, mode)
                # 이동 대기 — 플레이어 위치 갱신
                for _ in range(30):
                    if not self.running: return
                    time.sleep(0.1)
                    cx, cy = read_pos()
                    if cx is not None and max(abs(cx - wx), abs(cy - wy)) <= 2:
                        break
                px, py = read_pos() if self.running else (px, py)

    def _run_direct_walk(self, tx, ty, mode="좌클릭"):
        """직선 스텝 이동 (PathFinder 없을 때 fallback)"""
        MAX_STEP = 8
        while self.running:
            px, py = read_pos()
            if px is None: time.sleep(1); continue
            dx, dy = tx - px, ty - py
            dist = max(abs(dx), abs(dy))
            if dist <= 2:
                print(f"[NAV] 도착 ({px},{py})", flush=True)
                break
            ratio = min(1.0, MAX_STEP / dist)
            nx = px + int(dx * ratio)
            ny = py + int(dy * ratio)
            sx, sy = tile2scr(self.hwnd, px, py, nx, ny)
            print(f"[NAV] ({px},{py}) → ({nx},{ny}) 목표=({tx},{ty}) scr({sx},{sy})", flush=True)
            self._do_click(sx, sy, mode)
            if not self._sleep(1.0): break


# ═══════════════════════════════════════════
#  메인 앱
# ═══════════════════════════════════════════
class App:
    # F6 글로벌 핫키 — 현재 마우스 위치에 하드웨어 클릭
    HOTKEY_CLICK = 0xDD  # ] 키 (VK_OEM_6)
    HOTKEY_START = 0x74  # F5
    HOTKEY_STOP  = 0x1B  # Esc

    def __init__(self, root):
        self.root = root
        root.title("리니지 하드웨어 매크로 v4 — 동시성 큐")
        root.geometry("540x820")
        root.configure(bg=BG)
        root.resizable(True, True)

        s = ttk.Style(); s.theme_use('clam')
        s.configure('TNotebook', background=BG, borderwidth=0)
        s.configure('TNotebook.Tab', background=BG3, foreground=FG, padding=[14,5],
                    font=("맑은 고딕",10,"bold"))
        s.map('TNotebook.Tab', background=[('selected',ACCENT)],
              foreground=[('selected','black')])
        s.configure('TCombobox', fieldbackground=BG3, background=BG3, foreground=FG)

        # 상단 큐 상태 + 핫키 표시
        top = tk.Frame(root, bg=BG2, height=28)
        top.pack(fill='x', padx=4, pady=(4,0))
        self.qvar = tk.StringVar(value="입력큐: 대기중")
        tk.Label(top, textvariable=self.qvar, bg=BG2, fg=TEAL,
                 font=("Consolas",9)).pack(side='left', padx=8)
        self.hotkey_var = tk.StringVar(value="]=클릭 F5=시작 Esc=정지")
        tk.Label(top, textvariable=self.hotkey_var, bg=BG2, fg=PEACH,
                 font=("Consolas",9,"bold")).pack(side='right', padx=8)
        self._q_tick()

        # 글로벌 핫키 — GetAsyncKeyState 폴링
        self._hotkey_active = False
        self._hotkey_thread = threading.Thread(target=self._hotkey_loop, daemon=True)
        self._hotkey_thread.start()

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill='both', expand=True, padx=4, pady=4)
        self.tabs = []
        self._refresh()

        bf = tk.Frame(root, bg=BG)
        bf.pack(fill='x', padx=8, pady=4)
        tk.Button(bf, text="🔄 새로고침", command=self._refresh, bg=BLUE, fg="black",
                  font=("맑은 고딕",9,"bold"), relief='flat', cursor='hand2').pack(side='left', padx=4)
        tk.Button(bf, text="■ 전체정지", command=self._stop_all, bg=RED, fg="black",
                  font=("맑은 고딕",9,"bold"), relief='flat', cursor='hand2').pack(side='left', padx=4)

    def _hotkey_loop(self):
        """백그라운드 스레드 — 핫키 폴링"""
        while True:
            # ] = 하드웨어 클릭
            state = user32.GetAsyncKeyState(self.HOTKEY_CLICK)
            if state & 0x8000:
                if not self._hotkey_active:
                    self._hotkey_active = True
                    cx, cy = ctypes.c_long(), ctypes.c_long()
                    user32.GetCursorPos(ctypes.byref(cx), ctypes.byref(cy))
                    print(f"[HOTKEY] ] 키 감지 → 클릭 @ ({cx.value},{cy.value}) drv={hw._drv is not None}")
                    hw.click()
            else:
                self._hotkey_active = False

            # F5 = 현재 탭 시작
            if user32.GetAsyncKeyState(self.HOTKEY_START) & 0x8000:
                tab = self._current_tab()
                if tab and not tab.running:
                    print(f"[HOTKEY] F5 → 클라{tab.slot} 시작", flush=True)
                    tab._start()
                    time.sleep(0.3)

            # Esc = 현재 탭 정지
            if user32.GetAsyncKeyState(self.HOTKEY_STOP) & 0x8000:
                tab = self._current_tab()
                if tab and tab.running:
                    print(f"[HOTKEY] Esc → 클라{tab.slot} 정지", flush=True)
                    tab._stop()
                    time.sleep(0.3)

            time.sleep(0.02)

    def _q_tick(self):
        qsize = sched._q.qsize()
        self.qvar.set(f"입력큐: {qsize}건 대기" if qsize else "입력큐: 대기중")
        self.root.after(500, self._q_tick)

    def _refresh(self):
        for t in self.nb.tabs(): self.nb.forget(t)
        self.tabs.clear()
        pids = find_pids()
        if not pids:
            f = tk.Frame(self.nb, bg=BG)
            self.nb.add(f, text=" 게임없음 ")
            tk.Label(f, text="리니지 프로세스를 찾을 수 없습니다",
                     bg=BG, fg=RED, font=("맑은 고딕",14)).pack(expand=True)
            return
        for i, pid in enumerate(pids):
            self.tabs.append(ClientTab(self.nb, pid, i+1))

    def _current_tab(self):
        idx = self.nb.index(self.nb.select()) if self.nb.tabs() else -1
        return self.tabs[idx] if 0 <= idx < len(self.tabs) else None

    def _stop_all(self):
        for t in self.tabs: t._stop()


if __name__ == "__main__":
    try:
        root = tk.Tk()
        App(root)
        print("[OK] GUI 시작, mainloop 진입")
        root.mainloop()
    except Exception as e:
        print(f"[ERROR] GUI 예외: {e}")
        import traceback
        traceback.print_exc()
        input("Enter to exit...")
