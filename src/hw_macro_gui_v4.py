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
from tkinter import ttk
import ctypes, ctypes.wintypes as wt
import psutil, threading, time, random, os, sys, queue

if not ctypes.windll.shell32.IsUserAnAdmin():
    import subprocess
    subprocess.Popen([sys.executable], creationflags=0x00004000)
    sys.exit()

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

def read_pos():
    x,y=ctypes.c_int(),ctypes.c_int()
    if _scan.scan_read_player(ctypes.byref(x),ctypes.byref(y)):
        return x.value, y.value
    return None, None

def tile2scr(hwnd, px,py, tx,ty):
    cr=wt.RECT(); user32.GetClientRect(hwnd,ctypes.byref(cr))
    cw,ch=cr.right,cr.bottom
    pt=wt.POINT(0,0); user32.ClientToScreen(hwnd,ctypes.byref(pt))
    a=24.0*cw/800; b=12.0*ch/600
    cx=pt.x+cw//2; cy=pt.y+ch//2-int(ch*90/900)
    dx,dy=tx-px,ty-py
    return cx+int(a*(dx+dy)), cy+int(b*(dy-dx))

def _fg(hwnd):
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.03)

def _mv(x, y):
    user32.SetCursorPos(x, y)
    time.sleep(0.02)


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
        self._lbl(r1, "  딜레이:").pack(side='left')
        self.d_min = self._ent(r1, 3); self.d_min.insert(0, "1"); self.d_min.pack(side='left', padx=2)
        tk.Label(r1, text="~", bg=BG2, fg=FG2, font=("Consolas",10)).pack(side='left')
        self.d_max = self._ent(r1, 3); self.d_max.insert(0, "3"); self.d_max.pack(side='left', padx=2)
        self._lbl(r1, "초").pack(side='left')

        r2 = tk.Frame(ctrl, bg=BG2); r2.pack(fill='x', padx=4, pady=4)
        self._btn(r2, "▶ 시작", self._start, GREEN).pack(side='left', padx=4)
        self._btn(r2, "■ 정지", self._stop, RED).pack(side='left', padx=4)
        self.status_var = tk.StringVar(value="대기")
        tk.Label(r2, textvariable=self.status_var, bg=BG2, fg=YELLOW,
                 font=("Consolas",10)).pack(side='left', padx=8)

    # ── 스케줄러 래핑 입력 ──
    def _do_click(self, sx, sy, mode="좌클릭"):
        """스케줄러 큐에 클릭 요청"""
        def action():
            _fg(self.hwnd)
            _mv(sx, sy)
            if mode == "좌클릭":     hw.click()
            elif mode == "우클릭":   hw.right_click()
            elif mode == "더블클릭": hw.double_click()
        sched.submit(self.cid, action)

    def _do_type(self, text):
        """스케줄러 큐에 타이핑 요청"""
        def action():
            _fg(self.hwnd)
            hw.type_text(text)
            time.sleep(0.05)
            hw.key(0x0D)  # Enter
        sched.submit(self.cid, action)

    # ── UI 액션 ──
    def _get_pos(self):
        x, y = read_pos()
        self.pos_var.set(f"좌표: ({x},{y})" if x is not None else "좌표: 읽기실패")

    def _add_wp(self):
        t = self.wp_entry.get().strip()
        if t: self.wp_text.insert(tk.END, t+"\n"); self.wp_entry.delete(0, tk.END)

    def _add_cur(self):
        x, y = read_pos()
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

    def _add_cmd(self):
        t = self.cmd_type.get()
        x = self.cmd_x.get().strip(); y = self.cmd_y.get().strip()
        if not x or not y: return
        self.wp_text.insert(tk.END, f"{x},{y} # {t}\n")
        self.cmd_x.delete(0, tk.END); self.cmd_y.delete(0, tk.END)

    def _start(self):
        if self.running: return
        self.running = True; self.status_var.set("실행중")
        threading.Thread(target=self._run, daemon=True).start()

    def _stop(self):
        self.running = False; self.status_var.set("정지")

    def _sleep(self, sec):
        for _ in range(int(sec*10)):
            if not self.running: return False
            time.sleep(0.1)
        return True

    def _run(self):
        inf = self.loop_var.get() == "무제한"
        dmin = float(self.d_min.get() or 1)
        dmax = float(self.d_max.get() or 3)

        while self.running:
            # 1) 웨이포인트 이동
            wp = self.wp_text.get("1.0", tk.END).strip()
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

                    px, py = read_pos()
                    if px is None: time.sleep(1); continue

                    # 도착 판정 ±2타일
                    if abs(px-tx) <= 2 and abs(py-ty) <= 2: continue

                    sx, sy = tile2scr(self.hwnd, px, py, tx, ty)
                    self._do_click(sx, sy, mode)

                    if not self._sleep(random.uniform(dmin, dmax)): break

            # 2) 대사 전송
            for i in range(self.chat_lb.size()):
                if not self.running: break
                msg = self.chat_lb.get(i)
                self._do_type(msg)
                if not self._sleep(random.uniform(dmin, dmax)): break

            if not inf: break

        self.running = False
        self.status_var.set("완료")


# ═══════════════════════════════════════════
#  메인 앱
# ═══════════════════════════════════════════
class App:
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

        # 상단 큐 상태
        top = tk.Frame(root, bg=BG2, height=28)
        top.pack(fill='x', padx=4, pady=(4,0))
        self.qvar = tk.StringVar(value="입력큐: 대기중")
        tk.Label(top, textvariable=self.qvar, bg=BG2, fg=TEAL,
                 font=("Consolas",9)).pack(side='left', padx=8)
        self._q_tick()

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
