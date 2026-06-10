"""A* pathfinding using live memory passability check.
Standalone: python astar_path.py <dest_x> <dest_y> [--go]
Import:     from astar_path import PathFinder
"""
import ctypes, ctypes.wintypes as wt, psutil, struct, sys, os, time, heapq

def _res_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # 개발 모드: core/의 부모 (src/)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

kernel32 = ctypes.WinDLL('kernel32')
user32 = ctypes.WinDLL('user32')
SIZE_T = ctypes.c_size_t
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, SIZE_T, ctypes.POINTER(SIZE_T)]
kernel32.ReadProcessMemory.restype = wt.BOOL
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

MS_ADDR = 0x22E6656B150

# Driver-based stealth memory read
IOCTL_READ_MEMORY = 0x80002058  # CTL_CODE(0x8000, 0x816, METHOD_BUFFERED, FILE_ANY_ACCESS)
DRIVER_DEVICE = r'\\.\LcHide'

class _MemReadReq(ctypes.Structure):
    _fields_ = [('Pid', ctypes.c_ulong), ('Address', ctypes.c_ulonglong), ('Size', ctypes.c_ulong)]

def _open_driver():
    """Try to open driver handle. Returns handle or None."""
    try:
        h = kernel32.CreateFileW(DRIVER_DEVICE, 0xC0000000, 0, None, 3, 0, None)
        if h and h != ctypes.c_void_p(-1).value:
            return h
    except: pass
    return None

class _MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_ulonglong), ('AllocationBase', ctypes.c_ulonglong),
                 ('AllocationProtect', ctypes.c_ulong), ('_pad1', ctypes.c_ulong),
                 ('RegionSize', ctypes.c_ulonglong), ('State', ctypes.c_ulong),
                 ('Protect', ctypes.c_ulong), ('Type', ctypes.c_ulong), ('_pad2', ctypes.c_ulong)]

class _PathPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int), ('y', ctypes.c_int)]

CARDINAL_OFFSETS = {0: (0,0), 2: (1,0), 4: (0,1), 6: (-1,0)}
DIAG_TABLES = {
    1: [(1,-1), (0,0), (2,0), (1,0)],
    3: [(1,0), (0,1), (2,1), (1,1)],
    5: [(-1,0), (-2,1), (0,1), (-1,1)],
    7: [(-1,-1), (-2,0), (0,0), (-1,0)],
}
MOVE_DX = [0, 1, 1, 1, 0, -1, -1, -1]
MOVE_DY = [-1, -1, 0, 1, 1, 1, 0, -1]


class PathFinder:
    """A* pathfinder using game memory passability data."""

    def __init__(self, target_pid=None):
        self.h = None
        self.pid = None
        self.dll = None
        self.segs = {}
        self._cell_cache = {}
        self._rd = SIZE_T(0)
        self._drv = None  # driver handle for stealth reads
        self._ent_regions = []  # cached heap regions where entities were found
        self._discover_cache = []
        self._discover_cache_ts = 0.0
        self._tilemap_loaded = False
        self._tilemap_path = os.path.join(_res_dir(), 'maps', '4.txt')
        self._target_pid = target_pid  # 다클라: 특정 PID 지정
        self._init()

    def _init(self):
        if self._target_pid:
            self.pid = self._target_pid
        else:
            for p in psutil.process_iter(['pid', 'name']):
                if p.info['name'] and p.info['name'].lower() in ('lc.exe', 'sv.exe'):
                    self.pid = p.info['pid']; break
        if not self.pid: return
        self.h = kernel32.OpenProcess(0x1F0FFF, False, self.pid)
        self.dll = ctypes.CDLL(os.path.join(_res_dir(), 'scan_dll.dll'))
        self.dll.scan_init.restype = ctypes.c_int
        self.dll.scan_read_player.restype = ctypes.c_int
        self.dll.scan_read_player.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        self.dll.scan_get_player_addr.restype = ctypes.c_longlong
        self.dll.scan_read_hp.restype = ctypes.c_int
        self.dll.scan_read_hp.argtypes = [ctypes.POINTER(ctypes.c_int)]*4
        self.dll.scan_get_slots.restype = ctypes.c_int
        self.dll.scan_get_slots.argtypes = [ctypes.POINTER(ctypes.c_longlong), ctypes.c_int]
        self.dll.scan_discover_now.restype = ctypes.c_int
        self.dll.scan_read_state.restype = ctypes.c_int
        self.dll.scan_read_state.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        self.dll.map_set_path.argtypes = [ctypes.c_char_p]
        self.dll.map_load_tiles.restype = ctypes.c_int
        self.dll.map_get_tile.restype = ctypes.c_int
        self.dll.map_get_tile.argtypes = [ctypes.c_int, ctypes.c_int]
        self.dll.map_is_passable.restype = ctypes.c_int
        self.dll.map_is_passable.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.dll.map_find_path.restype = ctypes.c_int
        self.dll.map_find_path.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(_PathPoint), ctypes.c_int
        ]
        self.dll.scan_init(0, 0)
        self._player_ivt_off = 0x464220  # 고정 (VT 0xFCCA98용)
        self._ent_cache = []  # [(addr, type, name), ...] 주소 캐시
        self._slot_buf = (ctypes.c_longlong * 2048)()  # DLL 슬롯 버퍼
        self._drv = _open_driver()
        if self._drv:
            print("[OK] Driver stealth mode")
        self._ensure_tilemap_loaded()
        self.refresh_segs()

    def _ensure_tilemap_loaded(self):
        if self._tilemap_loaded or not self.dll:
            return self._tilemap_loaded
        if not os.path.exists(self._tilemap_path):
            return False
        try:
            self.dll.map_set_path(os.fsencode(self._tilemap_path))
            self._tilemap_loaded = self.dll.map_load_tiles() > 0
        except Exception:
            self._tilemap_loaded = False
        return self._tilemap_loaded

    def _find_path_via_tilemap(self, sx, sy, dx, dy):
        if not self._ensure_tilemap_loaded():
            return []
        buf = (_PathPoint * 65536)()
        try:
            n = int(self.dll.map_find_path(int(sx), int(sy), int(dx), int(dy), buf, len(buf)))
        except Exception:
            return []
        if n == 0:
            return []
        count = abs(n)
        if count <= 0:
            return []
        return [(buf[i].x, buf[i].y) for i in range(count)]

    def _rptr(self, a):
        buf = ctypes.create_string_buffer(8)
        if self._drv:
            req = _MemReadReq(Pid=self.pid, Address=a, Size=8)
            ret = ctypes.c_ulong(0)
            kernel32.DeviceIoControl(self._drv, IOCTL_READ_MEMORY,
                ctypes.byref(req), ctypes.sizeof(req), buf, 8, ctypes.byref(ret), None)
            if ret.value >= 8:
                return struct.unpack('<Q', buf.raw[:8])[0]
        # Fallback to ReadProcessMemory
        if kernel32.ReadProcessMemory(self.h, ctypes.c_void_p(a), buf, 8, ctypes.byref(self._rd)):
            return struct.unpack('<Q', buf.raw[:8])[0]
        return 0

    def _rmem(self, a, sz):
        buf = ctypes.create_string_buffer(sz)
        if self._drv:
            req = _MemReadReq(Pid=self.pid, Address=a, Size=sz)
            ret = ctypes.c_ulong(0)
            kernel32.DeviceIoControl(self._drv, IOCTL_READ_MEMORY,
                ctypes.byref(req), ctypes.sizeof(req), buf, sz, ctypes.byref(ret), None)
            if ret.value > 0:
                return buf.raw[:ret.value]
        # Fallback to ReadProcessMemory
        if kernel32.ReadProcessMemory(self.h, ctypes.c_void_p(a), buf, sz, ctypes.byref(self._rd)):
            return buf.raw[:self._rd.value]
        return None

    def read_player(self):
        x, y = ctypes.c_int(), ctypes.c_int()
        if self.dll.scan_read_player(ctypes.byref(x), ctypes.byref(y)):
            return x.value, y.value
        return 0, 0

    def read_state(self):
        """플레이어 상태 읽기. (state, action) 반환."""
        s, a = ctypes.c_int(), ctypes.c_int()
        if self.dll.scan_read_state(ctypes.byref(s), ctypes.byref(a)):
            return s.value, a.value
        return 0, 0

    def read_hp(self):
        """Returns (curHP, maxHP, curMP, maxMP) or (0,0,0,0)"""
        chp, mhp, cmp, mmp = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if self.dll.scan_read_hp(ctypes.byref(chp), ctypes.byref(mhp), ctypes.byref(cmp), ctypes.byref(mmp)):
            return chp.value, mhp.value, cmp.value, mmp.value
        return 0, 0, 0, 0

    # === 소모품 수량 ===
    _arrow_addr = 0
    _ARROW_VT_RVA = 0x100DC28  # 인벤토리 아이템 VT
    _ARROW_COUNT_OFF = 0x30     # 아이템 오브젝트 내 갯수 오프셋
    _ARROW_TEXT_OFF = 0x60      # 표시 텍스트 오프셋

    def find_arrow_auto(self):
        """VT 스캔으로 화살 아이템 자동 찾기. 갯수 반환 또는 -1."""
        psapi = ctypes.WinDLL('psapi')
        mods = (ctypes.c_ulonglong * 4)()
        needed = ctypes.c_ulong()
        psapi.EnumProcessModulesEx(self.h, mods, ctypes.sizeof(mods), ctypes.byref(needed), 3)
        vt_val = mods[0] + self._ARROW_VT_RVA
        vt_pat = struct.pack('<Q', vt_val)

        best_addr = 0
        best_count = 0
        mbi = _MBI()
        addr = 0x10000
        while addr < 0x7FFFFFFFFFFF:
            ret = kernel32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if ret == 0: break
            if mbi.State == 0x1000 and mbi.Protect in (0x02,0x04,0x06,0x20,0x40) and mbi.RegionSize < 0x10000000:
                data = self._rmem(mbi.BaseAddress, mbi.RegionSize)
                if data:
                    idx = 0
                    while True:
                        idx = data.find(vt_pat, idx)
                        if idx == -1: break
                        if idx % 8 == 0 and idx + 0x70 < len(data):
                            count = struct.unpack_from('<i', data, idx + self._ARROW_COUNT_OFF)[0]
                            if 1 <= count <= 9999:
                                # 화살인지 텍스트 확인
                                txt = data[idx + self._ARROW_TEXT_OFF:idx + self._ARROW_TEXT_OFF + 30]
                                txt_str = txt.split(b'\x00')[0].decode('ascii', 'ignore')
                                if str(count) in txt_str and count > best_count:
                                    best_addr = mbi.BaseAddress + idx + self._ARROW_COUNT_OFF
                                    best_count = count
                        idx += 8
            addr = mbi.BaseAddress + mbi.RegionSize
            if addr <= mbi.BaseAddress: break

        if best_addr:
            self._arrow_addr = best_addr
            print(f"[OK] 화살 자동 발견: {best_count}개 @ 0x{best_addr:X}")
        return best_count if best_addr else -1

    def snapshot_arrow_candidates(self):
        """공격 전: 영역별 raw 데이터 저장"""
        mbi = _MBI()
        addr = 0x10000
        self._arrow_regions = []  # [(base, old_data), ...]
        while addr < 0x7FFFFFFFFFFF:
            ret = kernel32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if ret == 0: break
            if mbi.State == 0x1000 and mbi.Type == 0x20000 and mbi.Protect == 0x04 and 0x1000 <= mbi.RegionSize <= 0x200000:
                data = self._rmem(mbi.BaseAddress, mbi.RegionSize)
                if data:
                    self._arrow_regions.append((mbi.BaseAddress, data))
            addr = mbi.BaseAddress + mbi.RegionSize
            if addr <= mbi.BaseAddress: break
        return len(self._arrow_regions)

    def detect_arrow_after_attack(self):
        """공격 후: 이전 스냅샷과 비교 → 정확히 1 감소한 주소 = 화살"""
        if not hasattr(self, '_arrow_regions') or not self._arrow_regions: return -1
        for base, old_data in self._arrow_regions:
            new_data = self._rmem(base, len(old_data))
            if not new_data: continue
            for i in range(0, len(old_data)-3, 4):
                old_v = struct.unpack_from('<i', old_data, i)[0]
                if not (10 <= old_v <= 9999): continue
                new_v = struct.unpack_from('<i', new_data, i)[0]
                if new_v == old_v - 1:
                    self._arrow_addr = base + i
                    self._arrow_regions = []
                    return new_v
        return -1

    def read_arrows(self):
        """화살 수량 읽기"""
        if not self._arrow_addr: return -1
        data = self._rmem(self._arrow_addr, 4)
        if data:
            v = struct.unpack('<i', data)[0]
            if 0 <= v <= 9999: return v
        # 주소 만료
        self._arrow_addr = 0
        return -1

    # === 소모품 (물약 등) ===
    _consumable_addrs = {}  # {이름: count_addr}
    _NPC_KEYWORDS = ['상인', '텔레포터', '가이드', '수호자', '전승자', '관리인', '상점',
                      '잡화', '무기', '대장', '마법', '방어', '보석', '식량', '펫',
                      '경매', '창고', '혈맹', '순찰', '경비']
    _npc_rels = set()  # 동적으로 발견된 NPC rel 값들
    _NPC_TID_MAX = 99
    _DISCOVER_CACHE_TTL = 2.0

    @staticmethod
    def _parse_item_name(display_text, count):
        """표시 텍스트에서 수량 제거 → 순수 아이템 이름. '체력 회복제 50' → '체력 회복제'"""
        if not display_text: return None
        for suffix in [f" {count}", f"({count})", f"×{count}", f"x{count}"]:
            if display_text.endswith(suffix):
                return display_text[:-len(suffix)].strip()
        import re
        m = re.match(r'(.+?)\s+\d+$', display_text)
        if m: return m.group(1).strip()
        return display_text.strip() or None

    def _is_npc_name(self, name):
        """이름으로 NPC 판별"""
        return any(kw in name for kw in self._NPC_KEYWORDS)

    def _read_entity_name(self, inner):
        """Read entity display name from inner object. Returns '' on failure."""
        p1 = self._rptr(inner + 24)
        if not p1 or p1 < 0x10000:
            return ''
        p2 = self._rptr(p1 + 32)
        if not p2 or p2 < 0x10000:
            return ''
        nb = self._rmem(p2 + 64, 64)
        if not nb:
            return ''
        raw = nb.split(b'\x00')[0]
        if len(raw) < 2:
            return ''
        try:
            return raw.decode('utf-8')
        except Exception:
            try:
                return raw.decode('cp949')
            except Exception:
                return ''

    def _classify_npc_or_mob(self, rel, inner):
        """Classify monster/NPC using type_id first, name fallback second."""
        tid_data = self._rmem(inner + 48, 4)
        tid = struct.unpack('<I', tid_data)[0] if tid_data else 0
        if 1 <= tid <= self._NPC_TID_MAX:
            return 'N', tid, self._read_entity_name(inner)
        if tid >= 100:
            return 'M', tid, ''
        name = self._read_entity_name(inner)
        if name and self._is_npc_name(name):
            return 'N', tid, name
        if name:
            return 'M', tid, name
        return '?', tid, ''

    def _classify_entity_record(self, addr, data, px, py, pa):
        """Classify one outer entity object from a memory snapshot."""
        if not data or len(data) < 0x8C:
            return None

        vt = struct.unpack_from('<Q', data, 0)[0]
        if not (0x7FF000000000 < vt < 0x7FFFFFFFFFFFFFF):
            return None

        vx = struct.unpack_from('<i', data, 104)[0]
        vy = struct.unpack_from('<i', data, 108)[0]
        if not (30000 < vx < 40000 and 30000 < vy < 40000):
            return None
        if px and py:
            if vx == px and vy == py:
                return None
            if max(abs(vx - px), abs(vy - py)) > 30:
                return None

        vx2 = struct.unpack_from('<i', data, 0x40)[0]
        vy2 = struct.unpack_from('<i', data, 0x44)[0]
        if not (30000 < vx2 < 40000 and 30000 < vy2 < 40000):
            return None
        if abs(vx - vx2) > 5 or abs(vy - vy2) > 5:
            return None

        inner = struct.unpack_from('<Q', data, 8)[0]
        if inner < 0x10000 or inner > 0x7FFFFFFFFFFF:
            return None

        ivt_raw = self._rptr(inner)
        if not (0x7FF000000000 < ivt_raw < 0x7FFFFFFFFFFFFFF):
            return None
        if not pa:
            return None

        ivt_off = pa - ivt_raw
        if ivt_off == self._player_ivt_off:
            return None

        rel = ivt_off - self._player_ivt_off
        t = '?'
        tid = 0
        mob_name = ''
        if rel in self._npc_rels:
            t, tid, mob_name = self._classify_npc_or_mob(rel, inner)
        elif rel == 0x38:
            t = 'C'
        elif rel == 0x148:
            t, tid, mob_name = self._classify_npc_or_mob(rel, inner)
        elif rel == 0x1A0:
            t = 'D'
        elif rel == 0x1B8:
            t = 'I'
        elif rel == -0x1C8 or rel == 0x250:
            tid_data = self._rmem(inner + 48, 4)
            tid = struct.unpack('<I', tid_data)[0] if tid_data else 0
            t = 'M'
        else:
            t, tid, mob_name = self._classify_npc_or_mob(rel, inner)

        if t == '?':
            return None
        if t == 'M' and tid > 100000:
            return None
        if t == 'M':
            if not mob_name:
                mob_name = self._read_entity_name(inner)
            o88 = struct.unpack_from('<I', data, 0x88)[0]
            if o88 > 1:
                return None
        elif t == 'N':
            self._npc_rels.add(rel)

        return (t, vx, vy, tid, mob_name, addr)

    def _discover_entities_fallback(self, px, py, pa):
        """Fallback full memory scan used when the DLL slot cache is empty."""
        vt_values = [vt for vt in getattr(self, '_ent_vts', []) if vt > 0x10000]
        if not vt_values:
            return [], []

        vt_pats = [struct.pack('<Q', vt) for vt in vt_values]
        ents = []
        new_cache = []
        seen_addrs = set()
        mbi = _MBI()
        addr = 0x10000
        while addr < 0x7FFFFFFFFFFF:
            ret = kernel32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if ret == 0:
                break
            if mbi.State == 0x1000 and mbi.Protect in (0x02, 0x04, 0x06, 0x20, 0x40) and mbi.RegionSize < 0x10000000:
                data = self._rmem(mbi.BaseAddress, mbi.RegionSize)
                if data:
                    seen_offsets = set()
                    for vt_pat in vt_pats:
                        start = 0
                        while True:
                            idx = data.find(vt_pat, start)
                            if idx == -1:
                                break
                            start = idx + 8
                            if idx % 8 != 0 or idx in seen_offsets or idx + 0x90 > len(data):
                                continue
                            seen_offsets.add(idx)
                            ent_addr = mbi.BaseAddress + idx
                            if ent_addr in seen_addrs:
                                continue
                            ent = self._classify_entity_record(ent_addr, data[idx:idx + 0x90], px, py, pa)
                            if not ent:
                                continue
                            seen_addrs.add(ent_addr)
                            ents.append(ent)
                            new_cache.append((ent_addr, ent[0], ent[4]))
            addr = mbi.BaseAddress + mbi.RegionSize
            if addr <= mbi.BaseAddress:
                break
        return ents, new_cache

    def find_consumables(self):
        """VT 스캔으로 모든 인벤 아이템 찾기. {이름: 수량} 반환."""
        psapi = ctypes.WinDLL('psapi')
        mods = (ctypes.c_ulonglong * 4)()
        needed = ctypes.c_ulong()
        psapi.EnumProcessModulesEx(self.h, mods, ctypes.sizeof(mods), ctypes.byref(needed), 3)
        vt_val = mods[0] + self._ARROW_VT_RVA
        vt_pat = struct.pack('<Q', vt_val)

        items = {}
        mbi = _MBI()
        addr = 0x10000
        while addr < 0x7FFFFFFFFFFF:
            ret = kernel32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if ret == 0: break
            if mbi.State == 0x1000 and mbi.Protect in (0x02,0x04,0x06,0x20,0x40) and mbi.RegionSize < 0x10000000:
                data = self._rmem(mbi.BaseAddress, mbi.RegionSize)
                if data:
                    idx = 0
                    while True:
                        idx = data.find(vt_pat, idx)
                        if idx == -1: break
                        if idx % 8 == 0 and idx + 0x80 < len(data):
                            count = struct.unpack_from('<i', data, idx + self._ARROW_COUNT_OFF)[0]
                            if 0 <= count <= 9999:
                                txt = data[idx + self._ARROW_TEXT_OFF:idx + self._ARROW_TEXT_OFF + 128]
                                txt_str = txt.split(b'\x00')[0].decode('utf-8', 'ignore')
                                name = self._parse_item_name(txt_str, count)
                                if name:
                                    count_addr = mbi.BaseAddress + idx + self._ARROW_COUNT_OFF
                                    # 같은 이름이면 수량 많은 것으로
                                    if name not in items or count > items[name][0]:
                                        items[name] = (count, count_addr)
                        idx += 8
            addr = mbi.BaseAddress + mbi.RegionSize
            if addr <= mbi.BaseAddress: break

        self._consumable_addrs = {name: ca for name, (_, ca) in items.items()}
        return {name: cnt for name, (cnt, _) in items.items()}

    def read_consumable(self, name):
        """특정 소모품 수량 읽기. 없으면 -1."""
        addr = self._consumable_addrs.get(name)
        if not addr: return -1
        data = self._rmem(addr, 4)
        if data:
            v = struct.unpack('<i', data)[0]
            if 0 <= v <= 9999: return v
        # 주소 만료
        if name in self._consumable_addrs:
            del self._consumable_addrs[name]
        return -1

    def find_potions(self):
        """물약 관련 아이템만 찾기. {이름: 수량} 반환."""
        all_items = self.find_consumables()
        POT_KW = ['회복제', '물약', '포션', '해독제', '치유', '체력', '고급', '강력', '용기', '악마',
                   '지혜', '속도', '와퍼', '엘븐']
        return {name: cnt for name, cnt in all_items.items()
                if any(kw in name for kw in POT_KW)}

    def find_npcs(self, max_dist=30, force_scan=False):
        """근처 NPC 찾기. [(name, x, y, tid, addr), ...] 반환."""
        ents = self.discover_entities(full_scan=force_scan)
        px, py = self.read_player()
        npcs = []
        for t, x, y, *rest in ents:
            if t != 'N': continue
            if max(abs(x - px), abs(y - py)) > max_dist: continue
            tid = rest[0] if len(rest) > 0 else 0
            name = rest[1] if len(rest) > 1 else ''
            addr = rest[2] if len(rest) > 2 else 0
            npcs.append((name, x, y, tid, addr))
        return npcs

    def _find_map_subsystem(self):
        """Find MapSubsystem via RTTI vtable search (works for any map/instance)."""
        # Step 1: Find LC.exe base
        psapi = ctypes.WinDLL('psapi')
        mods = (ctypes.c_ulonglong * 4)()
        needed = ctypes.c_ulong()
        psapi.EnumProcessModulesEx(self.h, mods, ctypes.sizeof(mods), ctypes.byref(needed), 3)
        exe_base = mods[0]

        # Step 2: COL at known RVAs (게임 버전별)
        col_addr = 0
        for rva in [0x11056F8, 0x1233208]:
            ca = exe_base + rva
            sig_data = self._rmem(ca, 4)
            if sig_data and struct.unpack('<I', sig_data)[0] == 1:
                col_addr = ca; break
        if not col_addr:
            col_addr = self._find_col_by_rtti(exe_base)
        if not col_addr: return 0

        # Step 3: Find vtable (8-byte COL pointer, search near COL)
        col_ptr_bytes = struct.pack('<Q', col_addr)
        vt_addr = 0
        for off in range(0, 0x4000000, 0x10000):
            data = self._rmem(exe_base + off, 0x10000)
            if not data: continue
            idx = data.find(col_ptr_bytes)
            if idx != -1:
                vt_addr = exe_base + off + idx + 8; break
        if not vt_addr: return 0

        # Step 4: Find heap instance with this vtable + valid tile_mgr
        vt_bytes = struct.pack('<Q', vt_addr)
        mbi = _MBI()
        addr = 0x10000
        while addr < 0x7FFFFFFFFFFF:
            ret = kernel32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if ret == 0: break
            if mbi.State == 0x1000 and mbi.Protect in (0x02,0x04,0x06,0x20,0x40) and mbi.RegionSize < 0x4000000:
                data = self._rmem(mbi.BaseAddress, mbi.RegionSize)
                if data:
                    fi = 0
                    while True:
                        fi = data.find(vt_bytes, fi)
                        if fi == -1: break
                        if fi % 8 == 0:
                            obj = mbi.BaseAddress + fi
                            tm = self._rptr(obj + 0x6E0)
                            if 0x100_00000000 < tm < 0x7FFF_FFFFFFFF:
                                th = self._rptr(tm + 0x08)
                                if th and 0x100_00000000 < th < 0x7FFF_FFFFFFFF:
                                    root = self._rptr(th + 0x08)
                                    if root and root != th and root > 0x10000:
                                        return obj
                        fi += 8
            addr = mbi.BaseAddress + mbi.RegionSize
            if addr <= mbi.BaseAddress: break
        return 0

    def _find_col_by_rtti(self, exe_base):
        """Fallback: find COL via RTTI string search."""
        pat = b'.?AVMapSubsystem@lineage@@'
        for off in range(0, 0x4000000, 0x100000):
            data = self._rmem(exe_base + off, 0x100000)
            if not data: continue
            idx = data.find(pat)
            if idx != -1:
                td = exe_base + off + idx - 0x10
                td_rva = td - exe_base
                td_rva_bytes = struct.pack('<I', td_rva & 0xFFFFFFFF)
                for o2 in range(0, 0x4000000, 0x100000):
                    d2 = self._rmem(exe_base + o2, 0x100000)
                    if not d2: continue
                    ci = d2.find(td_rva_bytes)
                    if ci != -1 and ci >= 12:
                        sig = struct.unpack_from('<I', d2, ci - 12)[0]
                        if sig == 1:
                            return exe_base + o2 + ci - 12
                break
        return 0

    def refresh_segs(self):
        # Try fixed MS_ADDR first
        ms = self._rptr(MS_ADDR)
        tile_mgr = self._rptr(ms + 0x6E0) if ms else 0

        # If fixed addr fails, auto-detect via RTTI
        if not tile_mgr or tile_mgr < 0x10000:
            if not hasattr(self, '_dynamic_ms') or not self._dynamic_ms:
                self._dynamic_ms = self._find_map_subsystem()
                if self._dynamic_ms:
                    print(f"[OK] MapSubsystem: 0x{self._dynamic_ms:012X}")
            if hasattr(self, '_dynamic_ms') and self._dynamic_ms:
                tile_mgr = self._rptr(self._dynamic_ms + 0x6E0)
            else:
                self.segs = {}; return

        if not tile_mgr or tile_mgr < 0x10000:
            self.segs = {}; return

        tree_hdr = self._rptr(tile_mgr + 0x08)
        root = self._rptr(tree_hdr + 0x08) if tree_hdr else 0
        self.segs = {}
        if not root or root < 0x10000 or root == tree_hdr: return
        visited = set()
        queue = [root]
        while queue:
            n = queue.pop(0)
            if n in visited or n < 0x10000 or n == tree_hdr: continue
            visited.add(n)
            nd = self._rmem(n, 64)
            if not nd or len(nd) < 64: continue
            left = struct.unpack_from('<Q', nd, 0)[0]
            right = struct.unpack_from('<Q', nd, 0x10)[0]
            if left > 0x10000 and left != tree_hdr: queue.append(left)
            if right > 0x10000 and right != tree_hdr: queue.append(right)
            if nd[0x19] != 0: continue
            key = struct.unpack_from('<I', nd, 0x20)[0]
            sx = (key >> 16) & 0xFFFF
            sy = key & 0xFFFF
            seg_data = struct.unpack_from('<Q', nd, 0x28)[0]
            if seg_data > 0x10000:
                self.segs[(sx, sy)] = seg_data

    def read_cell(self, wx, wy, dX, dY):
        X_half = wx * 2 - 0x8000 + dX
        Y = wy + dY
        ck = (X_half, Y)
        if ck in self._cell_cache:
            return self._cell_cache[ck]
        r8 = (X_half - 0x8000) >> 1
        halfX = r8 + 0x8000
        segY = (halfX >> 6) + 0x7E00
        segX = (Y >> 6) + 0x7E00
        k1 = (0x7F00 - segY) * 128 + X_half
        k2 = (0x7E00 - segX) * 64 + Y
        sd = self.segs.get((segY, segX))
        if not sd:
            self._cell_cache[ck] = None; return None
        cbi = self._rptr(sd)
        if cbi < 0x10000:
            self._cell_cache[ck] = None; return None
        rp = self._rptr(cbi + k2 * 24)
        if rp < 0x10000 or rp > 0x7FFFFFFFFFFF:
            self._cell_cache[ck] = None; return None
        cell = self._rptr(rp + k1 * 8)
        hi32 = cell >> 32
        self._cell_cache[ck] = hi32
        return hi32

    def has_line_of_sight(self, x1, y1, x2, y2):
        """(x1,y1)에서 (x2,y2)까지 직선상에 벽/울타리가 없는지 확인. True=쏠수있음."""
        dx = x2 - x1
        dy = y2 - y1
        steps = max(abs(dx), abs(dy))
        if steps == 0: return True
        for i in range(1, steps):
            t = i / steps
            cx = int(x1 + dx * t)
            cy = int(y1 + dy * t)
            # 해당 타일의 4방향 셀 데이터 확인
            for dX, dY in [(0,0), (1,0), (0,1)]:
                hi32 = self.read_cell(cx, cy, dX, dY)
                if hi32 is not None and (hi32 & 0x41) != 0:
                    return False  # 벽 or 울타리
        return True

    def update_blocked_tiles(self, exclude_names=None):
        """엔티티 위치 등록. NPC는 비켜주므로 blocked가 아닌 high-cost로 처리."""
        self._blocked_tiles = set()
        self._aggro_tiles = set()  # 몬스터 주변 (지나가면 어그로)
        self._npc_tiles = set()    # NPC 위치 (high-cost, 통과 가능)
        ents = self.discover_entities(full_scan=False)
        px, py = self.read_player()
        for t, x, y, *rest in ents:
            if x == px and y == py: continue
            if t in ('C', 'N', 'D'):
                # NPC/플레이어는 blocked가 아닌 high-cost (게임에서 비켜줌)
                self._npc_tiles.add((x, y))
            elif t == 'M':
                self._blocked_tiles.add((x, y))
                # 몬스터 주변 2타일을 어그로 존으로
                for dx in range(-2, 3):
                    for dy in range(-2, 3):
                        self._aggro_tiles.add((x+dx, y+dy))

    def is_passable(self, wx, wy, heading):
        # 목적지 타일에 몬스터 있으면 차단 (NPC는 통과 가능)
        nx = wx + MOVE_DX[heading]
        ny = wy + MOVE_DY[heading]
        if hasattr(self, '_blocked_tiles') and (nx, ny) in self._blocked_tiles:
            return False
        if heading in CARDINAL_OFFSETS:
            dX, dY = CARDINAL_OFFSETS[heading]
            hi32 = self.read_cell(wx, wy, dX, dY)
            if hi32 is None: return False
            return (hi32 & 0x41) == 0
        else:
            offsets = DIAG_TABLES[heading]
            cells = [self.read_cell(wx, wy, dX, dY) for dX, dY in offsets]
            if any(c is None for c in cells): return False
            for mask in [0x01, 0x40]:
                A = (cells[0] & mask) != 0
                B = (cells[1] & mask) != 0
                C = (cells[2] & mask) != 0
                D = (cells[3] & mask) != 0
                if heading in [3, 7]:
                    blocked = (A and (B or D)) or (B and C) or (C and D)
                else:
                    blocked = (A or B) and (C or D)
                if blocked: return False
            return True

    def astar(self, sx, sy, dx, dy, max_nodes=50000):
        if sx == dx and sy == dy:
            return [(sx, sy)]
        def heuristic(x, y):
            adx, ady = abs(x - dx), abs(y - dy)
            return max(adx, ady) * 10 + min(adx, ady) * 4

        open_set = [(heuristic(sx, sy), 0, sx, sy)]
        g_score = {(sx, sy): 0}
        came_from = {}
        closed = set()
        best_h = heuristic(sx, sy)
        best_node = (sx, sy)

        while open_set and len(closed) < max_nodes:
            f, g, cx, cy = heapq.heappop(open_set)
            if (cx, cy) in closed: continue
            closed.add((cx, cy))
            if cx == dx and cy == dy:
                path = []
                node = (dx, dy)
                while node in came_from:
                    path.append(node); node = came_from[node]
                path.append((sx, sy)); path.reverse()
                return path
            h = heuristic(cx, cy)
            if h < best_h:
                best_h = h; best_node = (cx, cy)
            for heading in range(8):
                if not self.is_passable(cx, cy, heading): continue
                nx = cx + MOVE_DX[heading]
                ny = cy + MOVE_DY[heading]
                if (nx, ny) in closed: continue
                cost = 14 if heading % 2 else 10
                # NPC 타일이면 비용 추가 (우회하지만 막히면 통과)
                if hasattr(self, '_npc_tiles') and (nx, ny) in self._npc_tiles:
                    cost += 15
                # 몬스터 어그로 존이면 비용 추가 (우회 유도)
                if hasattr(self, '_aggro_tiles') and (nx, ny) in self._aggro_tiles:
                    cost += 30
                ng = g + cost
                if ng < g_score.get((nx, ny), float('inf')):
                    g_score[(nx, ny)] = ng
                    came_from[(nx, ny)] = (cx, cy)
                    heapq.heappush(open_set, (ng + heuristic(nx, ny), ng, nx, ny))

        if best_node != (sx, sy):
            path = []
            node = best_node
            while node in came_from:
                path.append(node); node = came_from[node]
            path.append((sx, sy)); path.reverse()
            return path
        return [(sx, sy)]

    @staticmethod
    def simplify_path(path, max_step=7):
        if len(path) <= 1: return path
        waypoints = [path[0]]
        last_dir = None
        steps = 0
        for i in range(1, len(path)):
            d = (path[i][0] - path[i-1][0], path[i][1] - path[i-1][1])
            steps += 1
            if last_dir is not None and d != last_dir:
                waypoints.append(path[i-1]); steps = 1
            elif steps >= max_step:
                waypoints.append(path[i]); steps = 0
            last_dir = d
        if waypoints[-1] != path[-1]:
            waypoints.append(path[-1])
        return waypoints

    def reset_map(self):
        """맵 변경 시 호출 - MapSubsystem 재탐지"""
        self._dynamic_ms = 0
        self.segs = {}
        self._cell_cache.clear()

    def find_path(self, sx, sy, dx, dy, max_step=7, exclude_names=None):
        self._cell_cache.clear()
        self.refresh_segs()
        self.update_blocked_tiles(exclude_names)
        self._blocked_tiles.discard((dx, dy))
        t0 = time.time()
        path = self.astar(sx, sy, dx, dy)
        elapsed = time.time() - t0
        waypoints = self.simplify_path(path, max_step)
        return path, waypoints, elapsed

    def find_escape_target(self, px, py, min_dist=8):
        """끼였을 때 탈출 목표 좌표 찾기. NPC/엔티티 없는 빈 공간."""
        ents = self.discover_entities(full_scan=False)
        occupied = set()
        for t, x, y, *_ in ents:
            occupied.add((x, y))
        occupied.discard((px, py))

        # 8방향으로 min_dist만큼 떨어진 곳 중 가장 엔티티가 적은 방향
        best_target = None
        best_score = -1
        for h in range(8):
            ddx = MOVE_DX[h]
            ddy = MOVE_DY[h]
            tx = px + ddx * min_dist
            ty = py + ddy * min_dist
            # 해당 방향에 엔티티가 몇 개 있는지
            entity_count = 0
            for step in range(1, min_dist + 1):
                cx = px + ddx * step
                cy = py + ddy * step
                if (cx, cy) in occupied:
                    entity_count += 1
            score = min_dist - entity_count
            if score > best_score:
                best_score = score
                best_target = (tx, ty)
        return best_target

    @staticmethod
    def world_to_screen(hwnd, px, py, wx, wy):
        rect = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        pt = wt.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        cw, ch = rect.right, rect.bottom
        iso_a = 24.0 * cw / 800
        iso_b = 12.0 * ch / 600
        dx, dy = wx - px, wy - py
        scr_x = pt.x + cw // 2 + int(iso_a * (dx + dy))
        scr_y = pt.y + ch // 2 - int(ch * 90 / 900) + int(iso_b * (dy - dx))
        # 화면 안으로 클램프 (여백 20px)
        margin = 20
        scr_x = max(pt.x + margin, min(scr_x, pt.x + cw - margin))
        scr_y = max(pt.y + margin, min(scr_y, pt.y + ch - margin))
        return scr_x, scr_y

    def entity_click_screen(self, hwnd, px, py, wx, wy, x_off=0, y_off=0):
        """World/entity coordinate -> direct screen click point."""
        sx, sy = self.world_to_screen(hwnd, px, py, wx, wy)
        return sx + x_off, sy + y_off

    def npc_click_screen(self, hwnd, px, py, wx, wy, x_off=0, y_off=-15):
        """NPC/entity interaction click point."""
        return self.entity_click_screen(hwnd, px, py, wx, wy, x_off=x_off, y_off=y_off)

    def item_click_screen(self, hwnd, px, py, wx, wy, x_off=0, y_off=15):
        """Ground item click point."""
        return self.entity_click_screen(hwnd, px, py, wx, wy, x_off=x_off, y_off=y_off)

    def attack_screen(self, hwnd, px, py, wx, wy, x_off=0, y_off=-10):
        """Combat aim point."""
        return self.entity_click_screen(hwnd, px, py, wx, wy, x_off=x_off, y_off=y_off)

    def move_click_screen(self, hwnd, px, py, wx, wy, x_off=0, y_off=12):
        """Ground-biased movement click that avoids nearby NPC/shop sprites."""
        sx, sy = self.world_to_screen(hwnd, px, py, wx, wy)
        avoid_x = 0
        avoid_y = y_off
        try:
            ents = self.discover_entities(full_scan=False)
        except Exception:
            ents = []
        for t, ex, ey, *rest in ents:
            if t not in ('C', 'N', 'D'):
                continue
            if max(abs(ex - wx), abs(ey - wy)) > 2:
                continue
            esx, esy = self.world_to_screen(hwnd, px, py, ex, ey)
            dx = sx - esx
            dy = sy - esy
            if abs(dx) <= 28 and -42 <= dy <= 20:
                avoid_x += 14 if dx >= 0 else -14
                avoid_y = max(avoid_y, 18)
        avoid_x = max(-28, min(28, avoid_x))
        return sx + x_off + avoid_x, sy + avoid_y

    def click_tile(self, hwnd, esp, tx, ty):
        """정확한 타일 좌표 클릭"""
        px, py = self.read_player()
        px, py = self.read_player()
        if not px: return
        scr_x, scr_y = self.move_click_screen(hwnd, px, py, tx, ty)
        user32.SetCursorPos(scr_x, scr_y)
        time.sleep(0.05)
        esp.write(b"CLICK\n")
        time.sleep(0.1)

    # ===== Entity scanning (vtable-based) =====
    # 엔티티 VT RVA (exe 이미지 기준, 고정)
    _ENT_VT_RVAS = [0xFCCA98, 0xFCC290]  # 몬스터/NPC VT + 플레이어 VT

    def find_entity_vtable(self):
        """Auto-detect entity vtable. RVA 캐시 우선, 실패 시 좌표 검색."""
        # 1. 고정 RVA로 먼저 시도
        psapi = ctypes.WinDLL('psapi')
        mods = (ctypes.c_ulonglong * 4)()
        needed = ctypes.c_ulong()
        psapi.EnumProcessModulesEx(self.h, mods, ctypes.sizeof(mods), ctypes.byref(needed), 3)
        exe_base = mods[0]
        # 여러 VT 설정
        self._ent_vts = []
        for rva in self._ENT_VT_RVAS:
            vt = exe_base + rva
            test = self._rptr(vt)
            if test and 0x7FF000000000 < test < 0x7FFFFFFFFFFFFFF:
                self._ent_vts.append(vt)
        if self._ent_vts:
            self._ent_vt = self._ent_vts[0]

        # 2. Fallback: 플레이어 좌표로 힙 검색
        px, py = self.read_player()
        px, py = self.read_player()
        if not px: return
        coord_pat = struct.pack('<ii', px, py)
        mbi = _MBI()
        addr = 0x10000
        while addr < 0x7FFFFFFFFFFF:
            ret = kernel32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if ret == 0: break
            if mbi.State == 0x1000 and mbi.Protect in (0x02,0x04,0x06,0x20,0x40) and mbi.RegionSize < 0x10000000:
                data = self._rmem(mbi.BaseAddress, mbi.RegionSize)
                if data:
                    idx = data.find(coord_pat)
                    while idx != -1:
                        if idx >= 104:
                            vt = struct.unpack_from('<Q', data, idx - 104)[0]
                            if 0x7FF000000000 < vt < 0x7FFFFFFFFFFFFFF:
                                self._ent_vt = vt
                                if vt not in self._ent_vts:
                                    self._ent_vts.insert(0, vt)
                                PathFinder._ENT_VT_RVA = vt - exe_base
                                # 플레이어 inner vtable 기록
                                inner = struct.unpack_from('<Q', data, idx - 104 + 8)[0]
                                if inner > 0x10000:
                                    pivt = self._rptr(inner)
                                    pa = self.dll.scan_get_player_addr()
                                    if pivt > 0x7FF000000000:
                                        self._player_ivt_off = pa - pivt
                                return
                        idx = data.find(coord_pat, idx + 4)
            addr = mbi.BaseAddress + mbi.RegionSize
            if addr <= mbi.BaseAddress: break

    def discover_entities(self, full_scan=False):
        """C DLL 슬롯 캐시 기반 엔티티 열거. 백그라운드 스레드가 1초마다 새 슬롯 발견.
        full_scan=True: C에서 즉시 재발견 요청 (보통 불필요)."""
        now = time.time()
        if (not full_scan and self._discover_cache and
                now - self._discover_cache_ts <= self._DISCOVER_CACHE_TTL):
            return list(self._discover_cache)
        if full_scan:
            self.dll.scan_discover_now()

        # VT 세트 준비 (최초 1회)
        if not hasattr(self, '_ent_vts') or not self._ent_vts:
            self.find_entity_vtable()
        vt_set = set(getattr(self, '_ent_vts', []))

        px, py = self.read_player()
        pa = self.dll.scan_get_player_addr() if hasattr(self.dll, 'scan_get_player_addr') else 0

        # C DLL에서 모든 슬롯 주소 가져오기 (즉시, <1ms)
        n = self.dll.scan_get_slots(self._slot_buf, 2048)

        ents = []
        new_cache = []
        for i in range(n):
            addr = self._slot_buf[i]
            if addr <= 0: continue
            # 엔티티 외부 오브젝트 읽기 (VT~0x88 포함)
            data = self._rmem(addr, 0x90)
            if not data or len(data) < 0x8C: continue
            vt = struct.unpack_from('<Q', data, 0)[0]
            # VT: exe 이미지 범위 안이면 통과 (기존 vt_set 필터 완화)
            if not (0x7FF000000000 < vt < 0x7FFFFFFFFFFFFFF): continue
            vx = struct.unpack_from('<i', data, 104)[0]
            vy = struct.unpack_from('<i', data, 108)[0]
            if not (30000 < vx < 40000 and 30000 < vy < 40000): continue
            if vx == px and vy == py: continue
            # 플레이어에서 20타일 이상이면 스킵
            if max(abs(vx - px), abs(vy - py)) > 30: continue
            # 좌표 쌍 일치 검증 (+0x40/+0x44 vs +0x68/+0x6C)
            vx2 = struct.unpack_from('<i', data, 0x40)[0]
            vy2 = struct.unpack_from('<i', data, 0x44)[0]
            if not (30000 < vx2 < 40000 and 30000 < vy2 < 40000): continue
            if abs(vx - vx2) > 5 or abs(vy - vy2) > 5: continue
            # inner 포인터 유효성
            inner = struct.unpack_from('<Q', data, 8)[0]
            if inner < 0x10000 or inner > 0x7FFFFFFFFFFF: continue
            # inner VT 유효성 (exe 이미지 범위)
            ivt_raw = self._rptr(inner)
            if not (0x7FF000000000 < ivt_raw < 0x7FFFFFFFFFFFFFF): continue

            t = '?'; tid = 0; mob_name = ''
            if pa:
                ivt_off = pa - ivt_raw
                if ivt_off == self._player_ivt_off:
                    continue
                rel = ivt_off - self._player_ivt_off
                if rel == 0x38:
                    t = 'C'
                elif rel == 0x148:
                    t, tid, mob_name = self._classify_npc_or_mob(rel, inner)
                    if t == '?':
                        continue
                elif rel == 0x1A0:
                    t = 'D'
                elif rel == 0x1B8:
                    t = 'I'
                elif rel == -0x1C8 or rel == 0x250:
                    tid_data = self._rmem(inner + 48, 4)
                    tid = struct.unpack('<I', tid_data)[0] if tid_data else 0
                    t = 'M'
                else:
                    t, tid, mob_name = self._classify_npc_or_mob(rel, inner)
                    if t == '?':
                        continue

                # M: tid 너무 크면 화살/이펙트 → 스킵
                if t == 'M' and tid > 100000:
                    continue
                # M이면 이름 읽기
                if t == 'M':
                    if not mob_name:
                        mob_name = self._read_entity_name(inner)
            else:
                continue

            # M 시체 필터: OUTER+0x88 > 1 = 시체 (NPC는 제외)
            if t == 'M' and tid > 100000:
                continue
            if t == 'M':
                o88 = struct.unpack_from('<I', data, 0x88)[0]
                if o88 > 1:
                    continue

            ents.append((t, vx, vy, tid, mob_name, addr))
            new_cache.append((addr, t, mob_name))

        if new_cache:
            self._ent_cache = new_cache
        seen = set()
        deduped = [(t, x, y, tid, n, a) for t, x, y, tid, n, a in ents
                   if (x, y) not in seen and not seen.add((x, y))]
        self._discover_cache = deduped
        self._discover_cache_ts = time.time()
        return deduped

    def discover_entities(self, full_scan=False):
        """Return nearby entities using DLL slots first, full VT scan as fallback."""
        now = time.time()
        if (not full_scan and self._discover_cache and
                now - self._discover_cache_ts <= self._DISCOVER_CACHE_TTL):
            return list(self._discover_cache)
        if full_scan:
            self.dll.scan_discover_now()

        if not hasattr(self, '_ent_vts') or not self._ent_vts:
            self.find_entity_vtable()

        px, py = self.read_player()
        pa = self.dll.scan_get_player_addr() if hasattr(self.dll, 'scan_get_player_addr') else 0
        n = self.dll.scan_get_slots(self._slot_buf, 2048)

        def _collect_from_slots(slot_count):
            slot_ents = []
            slot_cache = []
            for i in range(slot_count):
                addr = self._slot_buf[i]
                if addr <= 0:
                    continue
                data = self._rmem(addr, 0x90)
                ent = self._classify_entity_record(addr, data, px, py, pa)
                if not ent:
                    continue
                slot_ents.append(ent)
                slot_cache.append((addr, ent[0], ent[4]))
            return slot_ents, slot_cache

        ents = []
        new_cache = []
        if n > 0:
            ents, new_cache = _collect_from_slots(n)
            if not ents:
                # DLL slots can remain populated while nearby records are stale.
                self.dll.scan_discover_now()
                n = self.dll.scan_get_slots(self._slot_buf, 2048)
                if n > 0:
                    ents, new_cache = _collect_from_slots(n)
        if not ents:
            ents, new_cache = self._discover_entities_fallback(px, py, pa)

        if new_cache:
            self._ent_cache = new_cache
        seen = set()
        deduped = [(t, x, y, tid, n, a) for t, x, y, tid, n, a in ents
                   if (x, y) not in seen and not seen.add((x, y))]
        self._discover_cache = deduped
        self._discover_cache_ts = time.time()
        return deduped

    # === 엔티티 죽음 상태 ===
    # VT 변경 = 엔티티 메모리 재사용 → 확실한 죽음
    # 좌표 범위 이탈 → 죽음/소멸
    # OUTER+0x88은 전투/어그로 상태이므로 사용하면 안 됨

    def is_entity_alive(self, ent_addr):
        """엔티티 생존 여부. ent_addr = outer object 주소.
        True=alive, False=dead/invalid."""
        data = self._rmem(ent_addr, 112)
        if not data or len(data) < 112: return False

        # 1. VT 변경 체크 (엔티티 재사용 = 확실히 죽음)
        vt = struct.unpack_from('<Q', data, 0)[0]
        vt_set = set(getattr(self, '_ent_vts', []))
        if vt_set and vt not in vt_set:
            return False

        # 2. 좌표 유효성
        x = struct.unpack_from('<i', data, 104)[0]
        y = struct.unpack_from('<i', data, 108)[0]
        if not (30000 < x < 40000 and 30000 < y < 40000):
            return False

        return True

    def read_entity_pos(self, ent_addr):
        """엔티티 좌표 읽기. (x, y) 또는 (0, 0)."""
        data = self._rmem(ent_addr + 104, 8)
        if not data or len(data) < 8: return 0, 0
        x, y = struct.unpack_from('<ii', data, 0)
        if 30000 < x < 40000 and 30000 < y < 40000:
            return x, y
        return 0, 0

    def fast_read_entities(self):
        """캐시된 주소에서 좌표만 재읽기 (<1ms). discover 대신 사용."""
        if not self._ent_cache: return []
        px, py = self.read_player()
        results = []
        alive = []
        vt_set = set(getattr(self, '_ent_vts', [self._ent_vt]))
        for addr, t, name in self._ent_cache:
            data = self._rmem(addr, 112)
            if not data or len(data) < 112: continue
            vt = struct.unpack_from('<Q', data, 0)[0]
            if vt not in vt_set: continue
            vx = struct.unpack_from('<i', data, 104)[0]
            vy = struct.unpack_from('<i', data, 108)[0]
            if not (30000 < vx < 40000 and 30000 < vy < 40000): continue
            if vx == px and vy == py: continue
            results.append((t, vx, vy, 0, name))
            alive.append((addr, t, name))
        self._ent_cache = alive
        return results

    def scan_monsters(self, px, py, max_dist=20):
        """Get nearby monsters. First call does full scan, subsequent use cached regions."""
        full = False
        ents = self.discover_entities(full_scan=full)
        monsters = []
        for t, ex, ey, *_ in ents:
            if t != 'M': continue
            if max(abs(ex-px), abs(ey-py)) > max_dist: continue
            monsters.append(('mob', ex, ey))
        return monsters

    def walk_path(self, hwnd, esp, dest_x, dest_y, max_step=7, arrive_dist=2, stop_fn=None):
        """A* 경로 따라 이동. 속도 측정 + 선클릭."""
        tile_speed = 0.77

        def sleep_or_stop(seconds):
            end = time.time() + max(0.0, seconds)
            while time.time() < end:
                if stop_fn and stop_fn():
                    return False
                remain = end - time.time()
                if remain <= 0:
                    break
                time.sleep(min(0.05, remain))
            return not (stop_fn and stop_fn())

        while True:
            if stop_fn and stop_fn(): return 'stopped'
            px, py = self.read_player()
            if not px:
                if not sleep_or_stop(0.5): return 'stopped'
                continue
            if max(abs(dest_x - px), abs(dest_y - py)) <= arrive_dist:
                return 'arrived'

            path, _, _ = self.find_path(px, py, dest_x, dest_y, max_step)
            if len(path) < 2: return 'no_path'
            wps = self.simplify_path(path, max_step)
            if len(wps) < 2: return 'no_path'

            wi = 1
            self.click_tile(hwnd, esp, wps[wi][0], wps[wi][1])

            last_pos = (px, py)
            last_time = time.time()
            stuck = 0

            while wi < len(wps):
                if stop_fn and stop_fn(): return 'stopped'
                if not sleep_or_stop(0.05): return 'stopped'
                nx, ny = self.read_player()
                if not nx: continue
                now = time.time()

                if max(abs(dest_x - nx), abs(dest_y - ny)) <= arrive_dist:
                    return 'arrived'

                moved = max(abs(nx-last_pos[0]), abs(ny-last_pos[1]))
                dt = now - last_time
                if moved > 0:
                    tile_speed = dt / moved
                    last_pos = (nx, ny)
                    last_time = now
                    stuck = 0
                else:
                    stuck += 0.05
                    if stuck >= 1.0:
                        # 막힘 → 랜덤 갈 수 있는 방향으로 탈출
                        import random as _rnd
                        dirs = list(range(8))
                        _rnd.shuffle(dirs)
                        for h in dirs:
                            if self.is_passable(nx, ny, h):
                                ex = nx + MOVE_DX[h] * 3
                                ey = ny + MOVE_DY[h] * 3
                                self.click_tile(hwnd, esp, ex, ey)
                                if not sleep_or_stop(1.0): return 'stopped'
                                break
                        break

                remaining = max(abs(wps[wi][0]-nx), abs(wps[wi][1]-ny))
                if remaining <= 1 and remaining > 0:
                    if not sleep_or_stop(tile_speed * 0.4): return 'stopped'
                    wi += 1
                    if wi < len(wps):
                        self.click_tile(hwnd, esp, wps[wi][0], wps[wi][1])
                elif remaining == 0:
                    wi += 1
                    if wi < len(wps):
                        self.click_tile(hwnd, esp, wps[wi][0], wps[wi][1])


# ===== Path overlay (lightweight, no entity scan) =====
import tkinter as tk

def show_path_overlay(pf, path, waypoints):
    """Show A* path on game window. ESC to close."""
    _r = [None]
    def _cb(hw, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hw, ctypes.byref(p))
        if p.value == pf.pid and user32.IsWindowVisible(hw): _r[0] = hw; return False
        return True
    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    hwnd = _r[0]
    if not hwnd: print("[!] Game window not found"); return

    wr = wt.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(wr))
    cr = wt.RECT(); user32.GetClientRect(hwnd, ctypes.byref(cr))
    cw, ch = cr.right, cr.bottom
    pt = wt.POINT(0, 0); user32.ClientToScreen(hwnd, ctypes.byref(pt))
    bx, by = pt.x - wr.left, pt.y - wr.top
    cx0 = bx + cw // 2
    cy0 = by + ch // 2 - int(ch * 90 / 900)
    iso_a = 24.0 * cw / 800
    iso_b = 12.0 * ch / 600
    ow, oh = wr.right - wr.left, wr.bottom - wr.top

    root = tk.Tk(); root.overrideredirect(True)
    root.geometry(f'{ow}x{oh}+{wr.left}+{wr.top}')
    root.attributes('-topmost', True)
    root.attributes('-transparentcolor', 'black')
    root.config(bg='black')
    root.update_idletasks()
    ohh = int(root.frame(), 16)
    s = user32.GetWindowLongW(ohh, -20)
    user32.SetWindowLongW(ohh, -20, s | 0x80000 | 0x20)
    cv = tk.Canvas(root, width=ow, height=oh, bg='black', highlightthickness=0)
    cv.pack()

    items = []
    def redraw():
        for it in items: cv.delete(it)
        items.clear()
        px, py = pf.read_player()
        if not px: root.after(500, redraw); return

        # Draw path
        prev = None
        for wx, wy in path:
            dx, dy = wx - px, wy - py
            if abs(dx) > 25 or abs(dy) > 25: prev = None; continue
            sx = cx0 + int(iso_a * (dx + dy))
            sy = cy0 + int(iso_b * (dy - dx))
            if prev:
                items.append(cv.create_line(prev[0], prev[1], sx, sy, fill='#00FF88', width=2))
            items.append(cv.create_oval(sx-2, sy-2, sx+2, sy+2, fill='#00FF88', outline=''))
            prev = (sx, sy)

        # Waypoints as bigger dots
        for wx, wy in waypoints:
            dx, dy = wx - px, wy - py
            if abs(dx) > 25 or abs(dy) > 25: continue
            sx = cx0 + int(iso_a * (dx + dy))
            sy = cy0 + int(iso_b * (dy - dx))
            items.append(cv.create_oval(sx-5, sy-5, sx+5, sy+5, fill='', outline='white', width=2))

        # Player dot
        items.append(cv.create_oval(cx0-4, cy0-4, cx0+4, cy0+4, fill='lime', outline='white', width=2))

        # Destination
        dw = path[-1] if path else (0,0)
        ddx, ddy = dw[0]-px, dw[1]-py
        if abs(ddx) <= 25 and abs(ddy) <= 25:
            dsx = cx0 + int(iso_a * (ddx + ddy))
            dsy = cy0 + int(iso_b * (ddy - ddx))
            items.append(cv.create_rectangle(dsx-6, dsy-6, dsx+6, dsy+6, fill='', outline='red', width=2))

        # Info
        items.append(cv.create_text(10, 10,
            text=f"({px},{py}) → ({dw[0]},{dw[1]}) {len(path)}칸 {len(waypoints)}wp\nESC=닫기",
            fill='yellow', anchor='nw', font=('Consolas', 10)))

        root.after(500, redraw)

    root.bind('<Escape>', lambda e: root.destroy())
    redraw()
    root.mainloop()


# ===== Standalone =====
if __name__ == '__main__':
    if not ctypes.windll.shell32.IsUserAnAdmin():
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{os.path.join(_res_dir(), "astar_path.py")}"', None, 1)
        sys.exit()

    pf = PathFinder()
    px, py = pf.read_player()
    print(f"Player: ({px}, {py})")

    if len(sys.argv) < 3:
        print("Usage: python astar_path.py <dest_x> <dest_y> [--go] [--port COM8]")
        sys.exit()

    dx, dy = int(sys.argv[1]), int(sys.argv[2])
    do_walk = '--go' in sys.argv

    print(f"Path: ({px},{py}) -> ({dx},{dy})")
    path, waypoints, elapsed = pf.find_path(px, py, dx, dy)
    print(f"A*: {len(path)} tiles, {len(waypoints)} wp, {elapsed:.2f}s")
    for i, (x, y) in enumerate(waypoints):
        prev = waypoints[i-1] if i > 0 else (px, py)
        d = max(abs(x - prev[0]), abs(y - prev[1]))
        print(f"  {i}: ({x},{y}) d={d}")

    if do_walk:
        import serial
        esp_port = 'COM8'
        if '--port' in sys.argv:
            idx = sys.argv.index('--port')
            if idx + 1 < len(sys.argv): esp_port = sys.argv[idx + 1]
        _r = [None]
        def _cb(hw, _):
            p = wt.DWORD()
            user32.GetWindowThreadProcessId(hw, ctypes.byref(p))
            if p.value == pf.pid and user32.IsWindowVisible(hw): _r[0] = hw; return False
            return True
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        hwnd = _r[0]
        esp = serial.Serial(port=esp_port, baudrate=115200, timeout=1)
        esp.dtr = False; esp.rts = False
        time.sleep(2); esp.reset_input_buffer()
        print(f"Walking to ({dx},{dy})...")
        result = pf.walk_path(hwnd, esp, dx, dy)
        print(f"Result: {result}")
        esp.close()
    else:
        # Show path overlay
        show_path_overlay(pf, path, waypoints)
