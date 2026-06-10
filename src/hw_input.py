"""Hardware-level input via kernel driver (MouClassServiceCallback)
커널 드라이버를 통해 마우스 입력을 하드웨어 레벨로 주입.
실제 마우스 입력과 100% 동일 — 안티치트 우회.
드라이버 없으면 mouse_event fallback.
"""
import ctypes
import ctypes.wintypes as wt
import time, random, threading

user32 = ctypes.WinDLL('user32')
kernel32 = ctypes.WinDLL('kernel32')

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_F1 = 0x70

IOCTL_MOUSE_INPUT = 0x80002060  # CTL_CODE(0x8000, 0x818, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_KBD_INPUT   = 0x80002064  # CTL_CODE(0x8000, 0x819, METHOD_BUFFERED, FILE_ANY_ACCESS)

class MOUSE_CLICK_REQUEST(ctypes.Structure):
    _fields_ = [("ButtonFlags", ctypes.c_ushort),
                ("DeltaX", ctypes.c_long),
                ("DeltaY", ctypes.c_long)]

class KBD_INPUT_REQUEST(ctypes.Structure):
    _fields_ = [("MakeCode", ctypes.c_ushort),
                ("Flags", ctypes.c_ushort)]

# VK → 스캔코드 매핑 (주요 키만)
VK_TO_SCAN = {
    0x70: 0x3B,  # F1
    0x71: 0x3C,  # F2
    0x72: 0x3D,  # F3
    0x73: 0x3E,  # F4
    0x74: 0x3F,  # F5
    0x75: 0x40,  # F6
    0x76: 0x41,  # F7
    0x77: 0x42,  # F8
    0x78: 0x43,  # F9
    0x79: 0x44,  # F10
    0x7A: 0x57,  # F11
    0x7B: 0x58,  # F12
    0x1B: 0x01,  # ESC
    0x0D: 0x1C,  # ENTER
    0x09: 0x0F,  # TAB
    0x11: 0x1D,  # CTRL
    0x10: 0x2A,  # SHIFT
    0x12: 0x38,  # ALT
    0x20: 0x39,  # SPACE
}

MOUSE_LEFT_DOWN = 0x0001
MOUSE_LEFT_UP = 0x0002
MOUSE_RIGHT_DOWN = 0x0008
MOUSE_RIGHT_UP = 0x0010

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]

class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]
    _anonymous_ = ("_u",)
    _fields_ = [("type", ctypes.c_ulong), ("_u", _U)]

user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint


class MouseLock:
    """프로세스 간 마우스 뮤텍스. 여러 autohunt.py가 마우스 안 꼬이게."""
    _mutex = None

    @staticmethod
    def acquire(timeout_ms=3000):
        if not MouseLock._mutex:
            MouseLock._mutex = kernel32.CreateMutexW(None, False, "Global\\LcHuntMouseLock")
        ret = kernel32.WaitForSingleObject(MouseLock._mutex, timeout_ms)
        return ret == 0  # WAIT_OBJECT_0

    @staticmethod
    def release():
        if MouseLock._mutex:
            kernel32.ReleaseMutex(MouseLock._mutex)


class HWInput:
    """커널 드라이버 기반 하드웨어 마우스 입력"""

    def __init__(self):
        self._drv = None
        try:
            kernel32.CreateFileW.restype = wt.HANDLE
            INVALID_HANDLE = wt.HANDLE(-1).value
            h = kernel32.CreateFileW(r'\\.\LcHide', 0xC0000000, 0, None, 3, 0, None)
            if not h or h == INVALID_HANDLE:
                # 드라이버 안 켜져있으면 자동 시작
                print("[*] 드라이버 로드 중...")
                import subprocess
                subprocess.run(['sc', 'start', 'lchide'], capture_output=True)
                time.sleep(1)
                h = kernel32.CreateFileW(r'\\.\LcHide', 0xC0000000, 0, None, 3, 0, None)
            if h and h != INVALID_HANDLE:
                self._drv = h
                # 콜백 초기화
                req = MOUSE_CLICK_REQUEST(ButtonFlags=0, DeltaX=0, DeltaY=0)
                out = (ctypes.c_uint64 * 2)()
                ret = ctypes.c_ulong(0)
                for attempt in range(3):
                    ok = kernel32.DeviceIoControl(self._drv, IOCTL_MOUSE_INPUT,
                        ctypes.byref(req), ctypes.sizeof(req),
                        ctypes.byref(out), 16, ctypes.byref(ret), None)
                    if ok:
                        print(f"[OK] 드라이버 마우스 (cb=0x{out[0]:X} dev=0x{out[1]:X})")
                        break
                    print(f"[!] 시도 {attempt+1}/3 실패 (cb=0x{out[0]:X} dev=0x{out[1]:X})")
                    time.sleep(0.5)
                else:
                    print("[!] 드라이버 마우스 콜백 실패 → SendInput fallback")
                    self._drv = None
            else:
                print("[!] 드라이버 없음 → mouse_event fallback")
        except:
            print("[!] 드라이버 없음 → mouse_event fallback")

    def _drv_kbd(self, scancode, up=False):
        """드라이버를 통한 하드웨어 키보드 입력"""
        if self._drv:
            req = KBD_INPUT_REQUEST(MakeCode=scancode, Flags=1 if up else 0)
            ret = ctypes.c_ulong(0)
            ok = kernel32.DeviceIoControl(self._drv, IOCTL_KBD_INPUT,
                ctypes.byref(req), ctypes.sizeof(req), None, 0, ctypes.byref(ret), None)
            if ok:
                return
            # 드라이버 실패 → keybd_event fallback
        # fallback: MapVirtualKey로 VK 찾아서 keybd_event
        vk = user32.MapVirtualKeyW(scancode, 1)  # MAPVK_VSC_TO_VK
        if vk:
            flags = KEYEVENTF_KEYUP if up else 0
            user32.keybd_event(vk, scancode, flags, 0)

    def _drv_mouse(self, flags, dx=0, dy=0):
        """드라이버를 통한 하드웨어 마우스 입력"""
        if self._drv:
            req = MOUSE_CLICK_REQUEST(ButtonFlags=flags, DeltaX=dx, DeltaY=dy)
            ret = ctypes.c_ulong(0)
            ok = kernel32.DeviceIoControl(self._drv, IOCTL_MOUSE_INPUT,
                ctypes.byref(req), ctypes.sizeof(req), None, 0, ctypes.byref(ret), None)
            if not ok:
                err = kernel32.GetLastError()
                print(f"[!] 드라이버 마우스 실패 err={err}, fallback")
                self._drv = None  # fallback으로 전환
                self._drv_mouse(flags, dx, dy)
        else:
            # SendInput fallback
            inp = INPUT()
            inp.type = 0  # INPUT_MOUSE
            inp.mi.dwFlags = flags  # MOUSE_LEFT_DOWN=1, UP=2 → MOUSEEVENTF_LEFTDOWN=2, UP=4
            if flags == MOUSE_LEFT_DOWN:
                inp.mi.dwFlags = 0x0002  # MOUSEEVENTF_LEFTDOWN
            elif flags == MOUSE_LEFT_UP:
                inp.mi.dwFlags = 0x0004  # MOUSEEVENTF_LEFTUP
            elif flags == MOUSE_RIGHT_DOWN:
                inp.mi.dwFlags = 0x0008
            elif flags == MOUSE_RIGHT_UP:
                inp.mi.dwFlags = 0x0010
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def click(self):
        """왼쪽 클릭 (하드웨어)"""
        self._drv_mouse(MOUSE_LEFT_DOWN)
        time.sleep(0.02)
        self._drv_mouse(MOUSE_LEFT_UP)

    def double_click(self):
        """더블클릭"""
        self.click()
        time.sleep(random.uniform(0.08, 0.15))
        self.click()

    def right_click(self):
        """우클릭"""
        self._drv_mouse(MOUSE_RIGHT_DOWN)
        time.sleep(0.02)
        self._drv_mouse(MOUSE_RIGHT_UP)

    def scroll(self, clicks=-1):
        """마우스 휠 스크롤. 음수=아래, 양수=위. 한 번에 1노치씩."""
        n = abs(clicks)
        direction = 1 if clicks > 0 else -1
        for _ in range(n):
            inp = INPUT()
            inp.type = 0
            inp.mi.dwFlags = 0x0800  # MOUSEEVENTF_WHEEL
            inp.mi.mouseData = ctypes.c_ulong(direction * 120 & 0xFFFFFFFF).value
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
            time.sleep(0.03)

    def type_text(self, text):
        """숫자/텍스트/한글 타이핑 — UNICODE 플래그로 모든 문자 지원"""
        KEYEVENTF_UNICODE = 0x0004
        for ch in str(text):
            cp = ord(ch)
            if cp < 128:
                # ASCII: 기존 방식 (드라이버 우선)
                vk = ord(ch.upper())
                scan = user32.MapVirtualKeyW(vk, 0)
                if scan:
                    self._drv_kbd(scan, up=False)
                    time.sleep(0.03)
                    self._drv_kbd(scan, up=True)
                else:
                    user32.keybd_event(vk, 0, 0, 0)
                    time.sleep(0.03)
                    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            else:
                # 한글/유니코드: SendInput KEYEVENTF_UNICODE
                inp_down = INPUT()
                inp_down.type = 1  # INPUT_KEYBOARD
                inp_down.ki.wScan = cp
                inp_down.ki.dwFlags = 0x0004  # KEYEVENTF_UNICODE
                user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(inp_down))
                time.sleep(0.03)
                inp_up = INPUT()
                inp_up.type = 1
                inp_up.ki.wScan = cp
                inp_up.ki.dwFlags = 0x0004 | 0x0002  # KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
                user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(inp_up))
            time.sleep(random.uniform(0.02, 0.06))

    def ctrl_click(self):
        """Ctrl + 왼쪽 클릭 홀드"""
        scan = VK_TO_SCAN.get(VK_CONTROL, 0x1D)
        self._drv_kbd(scan, up=False)
        time.sleep(0.03)
        self._drv_mouse(MOUSE_LEFT_DOWN)

    def ctrl_release(self):
        """Ctrl + 왼쪽 클릭 릴리즈"""
        self._drv_mouse(MOUSE_LEFT_UP)
        time.sleep(0.03)
        scan = VK_TO_SCAN.get(VK_CONTROL, 0x1D)
        self._drv_kbd(scan, up=True)

    def key(self, vk):
        """키 누르기 (드라이버 우선)"""
        scan = VK_TO_SCAN.get(vk, 0)
        if not scan:
            scan = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
        if scan:
            self._drv_kbd(scan, up=False)
            time.sleep(0.05)
            self._drv_kbd(scan, up=True)
        else:
            # 스캔코드 못 찾으면 keybd_event fallback
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def fkey(self, num):
        """펑션키 (F1~F12)"""
        self.key(VK_F1 + num - 1)

    def send(self, cmd):
        """ESP32 호환 명령어 처리"""
        cmd = cmd.strip()
        if cmd == "CLICK":
            self.click()
        elif cmd == "DBLCLICK":
            self.double_click()
        elif cmd == "RCLICK":
            self.right_click()
        elif cmd.startswith("SCROLL:"):
            self.scroll(int(cmd[7:]))
        elif cmd == "PRESS":
            self._drv_mouse(MOUSE_LEFT_DOWN)
        elif cmd == "RELEASE":
            self._drv_mouse(MOUSE_LEFT_UP)
        elif cmd == "CTRL_CLICK":
            self.ctrl_click()
        elif cmd == "CTRL_RELEASE":
            self.ctrl_release()
        elif cmd.startswith("KEY:"):
            k = cmd[4:]
            if k.startswith("F") and k[1:].isdigit():
                self.fkey(int(k[1:]))
            elif k == "ESC":
                self.key(0x1B)
            elif k == "ENTER":
                self.key(0x0D)
            elif k == "TAB":
                self.key(0x09)
        elif cmd == "PING":
            pass


if __name__ == '__main__':
    hw = HWInput()
    print("3초 후 클릭 테스트...")
    time.sleep(3)
    hw.click()
    print("클릭 완료!")
