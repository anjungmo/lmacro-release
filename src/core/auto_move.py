"""
자동 이동: ESP32 마우스+키보드 (게임 포커스 유지)
커서를 방향으로 이동 → 클릭 → 0.1초 후 ESC → 원위치
"""
import serial, ctypes, ctypes.wintypes as wt, psutil, time, sys, threading
import tkinter as tk
from tkinter import ttk

if not ctypes.windll.shell32.IsUserAnAdmin():
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{__file__}"', None, 1)
    sys.exit()

user32 = ctypes.WinDLL('user32')
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

def find_game_windows():
    pids = set()
    for p in psutil.process_iter(['pid', 'name']):
        if p.info['name'] and p.info['name'].lower() in ('lc.exe', 'sv.exe'):
            pids.add(p.info['pid'])
    results = []
    def cb(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            results.append((pid.value, hwnd))
        return True
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return results

def esp_cmd(esp, cmd):
    esp.write(f"{cmd}\n".encode())
    time.sleep(0.05)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Auto Move")
        self.root.geometry("350x420")
        self.root.attributes('-topmost', True)
        self.hwnd = None
        self.esp = None
        self.running = False
        self._games = []

        # ESP32
        ef = ttk.LabelFrame(self.root, text="ESP32")
        ef.pack(fill='x', padx=5, pady=3)
        self.com_var = tk.StringVar(value="COM8")
        ttk.Entry(ef, textvariable=self.com_var, width=6).pack(side='left', padx=3)
        ttk.Button(ef, text="Connect", command=self.connect_esp).pack(side='left', padx=3)
        self.esp_st = tk.StringVar(value="")
        ttk.Label(ef, textvariable=self.esp_st).pack(side='left', padx=3)

        # Game
        gf = ttk.Frame(self.root)
        gf.pack(fill='x', padx=5, pady=3)
        ttk.Button(gf, text="Scan", command=self.scan).pack(side='left')
        self.combo = ttk.Combobox(gf, width=15)
        self.combo.pack(side='left', padx=5)
        self.combo.bind('<<ComboboxSelected>>', self.on_select)

        self.status = tk.StringVar(value="Connect -> Scan -> 방향 -> START")
        ttk.Label(self.root, textvariable=self.status, font=('',10)).pack(pady=3)

        # 설정
        sf = ttk.Frame(self.root)
        sf.pack(fill='x', padx=5, pady=3)
        ttk.Label(sf, text="방향:").pack(side='left')
        self.dir_var = tk.StringVar(value="N")
        ttk.Combobox(sf, textvariable=self.dir_var, width=5,
                      values=['N','NE','E','SE','S','SW','W','NW']).pack(side='left', padx=3)
        ttk.Label(sf, text="간격:").pack(side='left', padx=(10,0))
        self.interval_var = tk.StringVar(value="0.8")
        ttk.Entry(sf, textvariable=self.interval_var, width=4).pack(side='left', padx=3)
        ttk.Label(sf, text="s").pack(side='left')

        sf2 = ttk.Frame(self.root)
        sf2.pack(fill='x', padx=5, pady=3)
        ttk.Label(sf2, text="ESC딜레이:").pack(side='left')
        self.esc_delay_var = tk.StringVar(value="0.1")
        ttk.Entry(sf2, textvariable=self.esc_delay_var, width=4).pack(side='left', padx=3)
        ttk.Label(sf2, text="클릭거리:").pack(side='left', padx=(10,0))
        self.dist_var = tk.StringVar(value="150")
        ttk.Entry(sf2, textvariable=self.dist_var, width=5).pack(side='left', padx=3)
        ttk.Label(sf2, text="px").pack(side='left')

        # 8방향 버튼 (1회 이동)
        main = tk.Frame(self.root)
        main.pack(pady=5)
        bs = {'font': ('', 12, 'bold'), 'width': 4, 'height': 1}
        dirs = [
            ('NW',0,0,'#81C784'),('N',0,1,'#4CAF50'),('NE',0,2,'#66BB6A'),
            ('W',1,0,'#FF9800'),(None,1,1,None),('E',1,2,'#2196F3'),
            ('SW',2,0,'#FF7043'),('S',2,1,'#f44336'),('SE',2,2,'#EF5350'),
        ]
        for d, r, c, color in dirs:
            if d:
                tk.Button(main, text=d, bg=color, fg='white', **bs,
                          command=lambda d=d: self.move_once(d)).grid(row=r, column=c, padx=2, pady=2)
            else:
                tk.Label(main, text="*", width=4).grid(row=r, column=c)

        # 자동 반복
        bf = ttk.Frame(self.root)
        bf.pack(pady=5)
        self.start_btn = tk.Button(bf, text="AUTO START", font=('',14,'bold'),
                                    bg='#4CAF50', fg='white', width=12,
                                    command=self.toggle)
        self.start_btn.pack()

        # 방향별 오프셋 (dx, dy 비율)
        self.dir_offsets = {
            'N':(0,-1), 'NE':(0.7,-0.7), 'E':(1,0), 'SE':(0.7,0.7),
            'S':(0,1), 'SW':(-0.7,0.7), 'W':(-1,0), 'NW':(-0.7,-0.7),
        }

    def connect_esp(self):
        self.esp_st.set("10초...")
        self.root.update()
        try:
            self.esp = serial.Serial()
            self.esp.port = self.com_var.get()
            self.esp.baudrate = 115200
            self.esp.dtr = False
            self.esp.rts = False
            self.esp.timeout = 1
            self.esp.open()
            time.sleep(10)
            self.esp.reset_input_buffer()
            esp_cmd(self.esp, "PING")
            time.sleep(1)
            r = self.esp.readline()
            self.esp_st.set("OK!" if b"PONG" in r else "?")
        except Exception as e:
            self.esp_st.set(str(e)[:20])

    def scan(self):
        self._games = find_game_windows()
        self.combo['values'] = [f"PID:{pid}" for pid, hwnd in self._games]
        if self._games:
            self.combo.current(0)
            self.on_select(None)

    def on_select(self, e):
        idx = self.combo.current()
        if 0 <= idx < len(self._games):
            self.hwnd = self._games[idx][1]
            self.status.set("Ready!")

    def do_move(self, direction):
        """ESP32로 1칸 이동: 원위치 저장→커서이동→클릭→ESC→SetCursorPos 복원"""
        dx, dy = self.dir_offsets[direction]
        dist = int(self.dist_var.get() or 150)
        esc_delay = float(self.esc_delay_var.get() or 0.1)
        px = int(dx * dist)
        py = int(dy * dist)

        # 원위치 저장
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        orig_x, orig_y = pt.x, pt.y

        # 커서 이동 + 클릭
        esp_cmd(self.esp, f"MOVE:{px},{py}")
        time.sleep(0.05)
        esp_cmd(self.esp, "CLICK")
        time.sleep(esc_delay)
        # ESC
        esp_cmd(self.esp, "ESC")
        time.sleep(0.05)
        # SetCursorPos로 정확히 원위치
        user32.SetCursorPos(orig_x, orig_y)

    def move_once(self, direction):
        if not self.esp:
            self.status.set("Connect!"); return
        self.do_move(direction)
        self.status.set(f"{direction} 1칸")

    def toggle(self):
        if self.running:
            self.running = False
            self.start_btn.config(text="AUTO START", bg='#4CAF50')
        else:
            if not self.esp:
                self.status.set("Connect!"); return
            self.running = True
            self.start_btn.config(text="STOP", bg='#f44336')
            threading.Thread(target=self.auto_loop, daemon=True).start()

    def auto_loop(self):
        while self.running:
            d = self.dir_var.get()
            interval = float(self.interval_var.get() or 0.8)
            self.do_move(d)
            self.root.after(0, lambda d=d: self.status.set(f"Auto: {d}"))
            time.sleep(interval)
        self.root.after(0, lambda: self.status.set("Stopped"))

    def run(self):
        self.root.mainloop()
        if self.esp: self.esp.close()

if __name__ == '__main__':
    App().run()
