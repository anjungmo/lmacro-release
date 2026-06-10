"""
사냥 엔진 — autohunt.py와 multi_hunt.py 공통 로직
단일클라/다클라 모두 사용 가능
"""
import time, random, struct, math, ctypes, ctypes.wintypes as wt, threading
from astar_path import PathFinder

user32 = ctypes.WinDLL('user32')

class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("hCursor", ctypes.c_void_p), ("ptScreenPos", wt.POINT)]

def get_cursor_handle():
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(ci)
    user32.GetCursorInfo(ctypes.byref(ci))
    return ci.hCursor


class HuntEngine:
    """사냥 핵심 로직. 입력은 input_fn을 통해 추상화."""

    def __init__(self, pf, hwnd, input_fn, log_fn, cfg=None):
        """
        pf: PathFinder 인스턴스
        hwnd: 게임 창 핸들
        input_fn: callable(cmd) — "CLICK", "PRESS", "RELEASE", "KEY:F5" 등
        log_fn: callable(msg)
        cfg: dict 설정
        """
        self.pf = pf
        self.hwnd = hwnd
        self.input = input_fn
        self.log = log_fn
        self.cfg = cfg or {}
        self.running = False

        # 상태
        self.monsters = []
        self.kill_positions = []
        self.skip_addrs = {}
        self.killed_addrs = {}
        self.normal_cursor = None
        self.action_count = 0
        self.last_path = []  # 오버레이 표시용 A* 경로

    def smooth_move(self, tx, ty, precise=False):
        """자연스러운 마우스 이동. precise=True면 오버슈트/흔들림 없이 정확히 이동"""
        cur = wt.POINT()
        user32.GetCursorPos(ctypes.byref(cur))
        sx, sy = cur.x, cur.y
        dist = math.hypot(tx - sx, ty - sy)
        if dist < 3:
            user32.SetCursorPos(tx, ty); return
        n = max(5, min(25, int(dist / 15))) + random.randint(-2, 2)
        curve_x = 0 if precise else random.uniform(-dist * 0.15, dist * 0.15)
        curve_y = 0 if precise else random.uniform(-dist * 0.1, dist * 0.1)
        for i in range(1, n + 1):
            if not self.running: return
            t = i / n
            ct = 1 - t
            mid_x = sx + (tx - sx) * 0.5 + curve_x
            mid_y = sy + (ty - sy) * 0.5 + curve_y
            mx = int(ct*ct*sx + 2*ct*t*mid_x + t*t*tx)
            my = int(ct*ct*sy + 2*ct*t*mid_y + t*t*ty)
            if not precise:
                mx += random.randint(-1, 1)
                my += random.randint(-1, 1)
            user32.SetCursorPos(mx, my)
            speed = 0.008 + 0.012 * math.sin(t * math.pi)
            if not self._interruptible_sleep(speed + random.uniform(-0.003, 0.005)): return
        if not precise and dist > 30 and random.random() < 0.4:
            user32.SetCursorPos(tx + random.randint(-4, 4), ty + random.randint(-3, 3))
            if not self._interruptible_sleep(random.uniform(0.02, 0.05)): return
        if not self.running: return
        user32.SetCursorPos(tx, ty)

    def click_tile(self, wx, wy, x_off=0, y_off=0, avoid_entities=True):
        """월드 좌표 클릭"""
        px, py = self.pf.read_player()
        if not px: return
        cr = self.cfg.get('click_rand', 5)
        if avoid_entities and hasattr(self.pf, 'move_click_screen'):
            move_y_off = self.cfg.get('move_y_off', 12)
            sx, sy = self.pf.move_click_screen(
                self.hwnd, px, py, wx, wy,
                x_off=x_off, y_off=y_off + move_y_off,
            )
            rx = sx + random.randint(-cr, cr)
            ry = sy + random.randint(-cr, cr)
        else:
            sx, sy = self.pf.entity_click_screen(
                self.hwnd, px, py, wx, wy, x_off=x_off, y_off=y_off
            )
            rx = sx + random.randint(-cr, cr)
            ry = sy + random.randint(-cr, cr)
        self.smooth_move(rx, ry)
        if not self.running: return
        if not self._interruptible_sleep(random.uniform(0.01, 0.12)): return
        self.input("CLICK")

    def aim_and_fire(self, tent_addr, tx, ty, cmd="CLICK"):
        """커서=칼 될 때까지 추적 → 발사. 실패해도 강제 클릭."""
        atk_x = self.cfg.get('atk_x_off', 0)
        atk_y = self.cfg.get('atk_y_off', -10)
        npx, npy = self.pf.read_player()
        ex, ey = tx, ty
        if tent_addr:
            r = self.pf.read_entity_pos(tent_addr)
            if r[0]: ex, ey = r
        if not npx or ex <= 30000: return None, None
        sx, sy = self.pf.attack_screen(self.hwnd, npx, npy, ex, ey, atk_x, atk_y)
        self.smooth_move(sx, sy)
        # 커서=칼 확인 (7회, 위아래 탐색)
        best_sx, best_sy = sx, sy
        for adj in range(7):
            npx2, npy2 = self.pf.read_player()
            if tent_addr:
                r2 = self.pf.read_entity_pos(tent_addr)
                if r2[0]: ex, ey = r2
            if npx2:
                # 위아래 교차 탐색: 0, -4, +4, -8, +8, -12, +12
                y_off = atk_y + (-(adj+1)//2 * 4 if adj % 2 == 1 else (adj//2) * 4)
                best_sx, best_sy = self.pf.attack_screen(self.hwnd, npx2, npy2, ex, ey, atk_x, y_off)
                user32.SetCursorPos(best_sx, best_sy)
            time.sleep(0.02)
            if self.normal_cursor and get_cursor_handle() != self.normal_cursor:
                self.input(cmd)
                return ex, ey
        # 커서 체크 실패해도 강제 클릭 (거리 가까우면)
        dist = max(abs(ex - npx), abs(ey - npy)) if npx else 99
        if dist <= 3:
            user32.SetCursorPos(best_sx, best_sy)
            time.sleep(0.03)
            self.input(cmd)
            return ex, ey
        return None, None

    def scan_monsters(self, excl_names=None, incl_list=None, excl_list=None):
        """몬스터 스캔 + 필터"""
        # 주기적 full_scan (15초마다, 오버헤드 줄임)
        if not hasattr(self, '_full_scan_t'): self._full_scan_t = 0
        do_full = time.time() - self._full_scan_t > 15
        if do_full: self._full_scan_t = time.time()
        ents = self.pf.discover_entities(full_scan=do_full)
        px, py = self.pf.read_player()
        if not px: return []
        monsters = []
        now_t = time.time()
        for t, x, y, *r in ents:
            if t != 'M' or max(abs(x-px), abs(y-py)) > 30: continue
            etid = r[0] if len(r) > 0 else 0
            name = r[1] if len(r) > 1 else ''
            ea = r[2] if len(r) > 2 else 0
            # 포함 필터
            if incl_list:
                matched = any((itid and etid==itid) or (iname and name==iname)
                             for itid, iname in incl_list)
                if not matched: continue
            # 제외 필터
            if excl_list:
                excluded = any((xtid and etid==xtid) or (xname and name==xname)
                              for xtid, xname in excl_list)
                if excluded: continue
            # 스킵 캐시
            if ea and ea in self.skip_addrs and now_t - self.skip_addrs[ea] < 10:
                continue
            if ea and ea in self.killed_addrs and now_t - self.killed_addrs[ea] < 10:
                continue
            monsters.append((x, y, ea))
        monsters.sort(key=lambda m: max(abs(m[0]-px), abs(m[1]-py)))
        return monsters

    def combat_loop(self, tx, ty, tent_addr):
        """전투: CLICK → PRESS → 죽음 대기 → RELEASE"""
        atk_x = self.cfg.get('atk_x_off', 0)
        atk_y = self.cfg.get('atk_y_off', -10)

        # 좌표/거리 계산
        px, py = self.pf.read_player()
        tdist = max(abs(tx-px), abs(ty-py)) if px else 99

        # 접근
        atk_range = self.cfg.get('atk_range', 11)
        if tdist > atk_range:
            self.log(f"[MOVE] ({tx},{ty}) d={tdist}")
            path, _, _ = self.pf.find_path(px, py, tx, ty)
            self.last_path = path

            wps = PathFinder.simplify_path(path, 7)
            # 여러 WP를 연속 클릭 (도착 전에 다음 클릭 → 끊김 없음)
            for wi in range(1, len(wps)):
                if not self.running: return 'stopped'
                self.click_tile(wps[wi][0], wps[wi][1])
                for _ in range(30):
                    time.sleep(0.1)
                    if not self.running: return 'stopped'
                    nx, ny = self.pf.read_player()
                    if not nx: break
                    if max(abs(tx-nx), abs(ty-ny)) <= atk_range: break
                    # 다음 WP 3타일 이내면 미리 다음 클릭
                    if max(abs(wps[wi][0]-nx), abs(wps[wi][1]-ny)) <= 5: break
                nx, ny = self.pf.read_player()
                if nx and max(abs(tx-nx), abs(ty-ny)) <= atk_range: break
            return 'approach'

        # 시야 체크 — 실패 시 A*로 우회 (LOS 확보될 때까지 이동)
        if not self.pf.has_line_of_sight(px, py, tx, ty):
            self.log(f"[LOS] 우회 ({tx},{ty})")
            path, _, _ = self.pf.find_path(px, py, tx, ty)
            self.last_path = path

            wps = PathFinder.simplify_path(path, 5)
            for wi in range(1, len(wps)):
                if not self.running: return 'stopped'
                self.click_tile(wps[wi][0], wps[wi][1])
                for _ in range(25):
                    time.sleep(0.1)
                    if not self.running: return 'stopped'
                    nx, ny = self.pf.read_player()
                    if not nx: break
                    # LOS 확보되면 즉시 공격으로
                    if self.pf.has_line_of_sight(nx, ny, tx, ty):
                        px, py = nx, ny
                        break
                    if max(abs(wps[wi][0]-nx), abs(wps[wi][1]-ny)) <= 5: break
                # LOS 확보됐으면 우회 종료
                px2, py2 = self.pf.read_player()
                if px2 and self.pf.has_line_of_sight(px2, py2, tx, ty):
                    px, py = px2, py2
                    break
            else:
                return 'approach'

        # 첫 발 CLICK
        result = self.aim_and_fire(tent_addr, tx, ty, "CLICK")
        if result[0] is None:
            self.log(f"[AIM] 커서 안 바뀜 ({tx},{ty}) d={tdist}")
            if tent_addr: self.skip_addrs[tent_addr] = time.time()
            return 'skip'
        tx, ty = result
        self.log(f"[ATK] ({tx},{ty})")

        # PRESS 홀드
        time.sleep(0.2)
        self.aim_and_fire(tent_addr, tx, ty, "PRESS")

        # 죽음 대기
        atk_state0, _ = self.pf.read_state()
        for tick in range(60):
            time.sleep(0.1)
            if not self.running: break
            chp = self.pf.read_hp()[0]
            if chp <= 0: self.input("RELEASE"); break

            # VT 체크
            if tent_addr and not self.pf.is_entity_alive(tent_addr):
                self.log("[DEAD]"); break

            # 상태 변화
            if tick >= 3:
                st, _ = self.pf.read_state()
                if st != atk_state0:
                    if tent_addr and not self.pf.is_entity_alive(tent_addr):
                        self.log("[DEAD]"); break
                    self.input("RELEASE")
                    time.sleep(0.05)
                    self.aim_and_fire(tent_addr, tx, ty, "CLICK")
                    time.sleep(0.15)
                    self.aim_and_fire(tent_addr, tx, ty, "PRESS")
                    atk_state0 = st

            # 시체 체크 + 커서 추적
            if tent_addr:
                outer = self.pf._rmem(tent_addr, 0x90)
                if outer and len(outer) >= 0x8C:
                    if struct.unpack_from('<I', outer, 0x88)[0] > 1:
                        self.log("[DEAD] 시체"); break
                    ex = struct.unpack_from('<i', outer, 0x68)[0]
                    ey = struct.unpack_from('<i', outer, 0x6C)[0]
                    if ex and ey and 30000 < ex < 40000:
                        tx, ty = ex, ey

        self.input("RELEASE")
        if tent_addr: self.killed_addrs[tent_addr] = time.time()

        # 킬 후 아이템 줍기
        if self.cfg.get('loot', True):
            self.loot_nearby(tx, ty)

        return 'killed'

    def loot_nearby(self, kill_x=0, kill_y=0):
        """근처 아이템(t=='I') 줍기. kill 좌표 근처 우선."""
        px, py = self.pf.read_player()
        if not px: return
        ents = self.pf.discover_entities(full_scan=False)
        items = []
        for t, x, y, *r in ents:
            if t != 'I': continue
            d = max(abs(x - px), abs(y - py))
            if d > 15: continue
            items.append((x, y, d))
        if not items: return
        # 킬 좌표에 가까운 순 → 플레이어에 가까운 순
        if kill_x and kill_y:
            items.sort(key=lambda i: max(abs(i[0]-kill_x), abs(i[1]-kill_y)))
        else:
            items.sort(key=lambda i: i[2])
        item_y_off = self.cfg.get('item_y_off', 15)
        for ix, iy, d in items:
            if not self.running: break
            self.log(f"[LOOT] ({ix},{iy}) d={d}")
            if d > 2:
                path, _, _ = self.pf.find_path(px, py, ix, iy)
    
                wps = PathFinder.simplify_path(path, 5)
                if len(wps) >= 2:
                    self.click_tile(wps[1][0], wps[1][1])
                    for _ in range(25):
                        time.sleep(0.1)
                        if not self.running: return
                        nx, ny = self.pf.read_player()
                        if nx and max(abs(ix-nx), abs(iy-ny)) <= 2: break
            # 줍기 전 재확인 — 아직 내 아이템인지 (슬롯에 I로 있는지)
            still_there = False
            ents2 = self.pf.discover_entities(full_scan=False)
            for t2, x2, y2, *_ in ents2:
                if t2 == 'I' and abs(x2 - ix) <= 1 and abs(y2 - iy) <= 1:
                    still_there = True; break
            if not still_there:
                self.log(f"[LOOT] ({ix},{iy}) 사라짐/남의것 → 스킵")
                continue
            # 아이템 클릭
            px2, py2 = self.pf.read_player()
            if px2:
                sx, sy = self.pf.item_click_screen(self.hwnd, px2, py2, ix, iy, y_off=item_y_off)
                user32.SetCursorPos(sx, sy)
                time.sleep(0.05)
                self.input("CLICK")
                time.sleep(0.4)
                px, py = self.pf.read_player()

    def patrol_step(self, cur_wp):
        """순찰 한 스텝"""
        px, py = self.pf.read_player()
        if not px: return
        wr = self.cfg.get('wander_range', 4)
        wp_pct = self.cfg.get('wander_pause', 40) / 100.0
        wander_x = cur_wp[0] + random.randint(-wr, wr)
        wander_y = cur_wp[1] + random.randint(-wr, wr)
        path, _, _ = self.pf.find_path(px, py, wander_x, wander_y)
        self.last_path = path
        from astar_path import PathFinder
        wps = PathFinder.simplify_path(path, random.randint(3, 6))
        if len(wps) < 2: return
        if random.random() < wp_pct:
            time.sleep(random.uniform(0.2, 1.5))
        cr = self.cfg.get('click_rand', 5)
        # 여러 WP 연속 클릭 (도착 전에 다음 → 끊김 없음)
        for wi in range(1, len(wps)):
            if not self.running: return
            ox = random.randint(-cr, cr) if cr else 0
            oy = random.randint(-cr, cr) if cr else 0
            self.click_tile(wps[wi][0] + ox, wps[wi][1] + oy)
            prev_pos = None
            stuck_count = 0
            for _ in range(20):
                time.sleep(0.1)
                if not self.running: return
                nx, ny = self.pf.read_player()
                if not nx: break
                # 막혔는지 체크 (2틱 연속 같은 위치면 재경로)
                if prev_pos and prev_pos == (nx, ny):
                    stuck_count += 1
                    if stuck_count >= 3:
                        self.log("[STUCK] 재경로")
                        return  # 메인루프로 복귀 → 다시 find_path
                else:
                    stuck_count = 0
                prev_pos = (nx, ny)
                # 최종 목표 도착
                if max(abs(cur_wp[0]-nx), abs(cur_wp[1]-ny)) <= 3: return
                # 다음 WP 5타일 이내면 미리 다음 클릭
                if max(abs(wps[wi][0]-nx), abs(wps[wi][1]-ny)) <= 5: break
                # 이동 중 몬스터 체크
                monsters = self.scan_monsters()
                if monsters:
                    self.monsters = monsters
                    return

    def check_hp(self):
        """HP 체크. 화살/물약 수량도 갱신."""
        chp, mhp, cmp, mmp = self.pf.read_hp()
        if mhp <= 0: return True
        if chp <= 0: return False
        self.hp_pct = chp * 100 // mhp
        self.mp_pct = cmp * 100 // mmp if mmp > 0 else 100
        # 화살 수량
        arrows = self.pf.read_arrows()
        if arrows < 0 and not hasattr(self, '_arrow_tried'):
            arrows = self.pf.find_arrow_auto()
            self._arrow_tried = True
        self.arrows = arrows
        # 물약 수량 (최초 1회 스캔, 이후 캐시 주소에서 읽기)
        if not hasattr(self, '_pot_addrs'):
            pots = self.pf.find_potions()
            self._pot_addrs = {name: addr for name, addr in self.pf._consumable_addrs.items() if name in pots}
        self.potion_counts = {}
        for name in list(self._pot_addrs.keys()):
            cnt = self.pf.read_consumable(name)
            if cnt >= 0:
                self.potion_counts[name] = cnt
            else:
                del self._pot_addrs[name]  # 만료된 주소 제거
        return True

    def play_macro(self, actions):
        """녹화된 매크로 시퀀스 재생.
        actions: [{'type':'ui_click','rx':0.5,'ry':0.3}, ...]
        좌표는 클라이언트 비율(0.0~1.0)."""
        if not actions: return

        def _client_pos():
            cr = wt.RECT(); user32.GetClientRect(self.hwnd, ctypes.byref(cr))
            pt = wt.POINT(0, 0); user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
            return pt.x, pt.y, cr.right, cr.bottom

        last_anchor = None

        def _coerce_int(value, default=None):
            if value in (None, ''):
                return default
            try:
                return int(value)
            except (TypeError, ValueError):
                try:
                    return int(float(value))
                except (TypeError, ValueError):
                    return default

        def _coerce_float(value, default=None):
            if value in (None, ''):
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def _normalize_anchor(source):
            if not source:
                return None
            anchor = {}
            rx = _coerce_float(source.get('rx'), None)
            ry = _coerce_float(source.get('ry'), None)
            cx = _coerce_int(source.get('cx'), None)
            cy = _coerce_int(source.get('cy'), None)
            cw0 = _coerce_int(source.get('cw'), None)
            ch0 = _coerce_int(source.get('ch'), None)
            if rx is not None:
                anchor['rx'] = rx
            if ry is not None:
                anchor['ry'] = ry
            if cx is not None:
                anchor['cx'] = cx
            if cy is not None:
                anchor['cy'] = cy
            if cw0 is not None:
                anchor['cw'] = cw0
            if ch0 is not None:
                anchor['ch'] = ch0
            return anchor or None

        def _extract_anchor(step):
            return _normalize_anchor(step)

        def _screen_from_anchor(anchor):
            anchor = _normalize_anchor(anchor)
            if not anchor:
                return None, None
            cx = _coerce_int(anchor.get('cx'), None)
            cy = _coerce_int(anchor.get('cy'), None)
            rec_cw = _coerce_int(anchor.get('cw'), 0) or 0
            rec_ch = _coerce_int(anchor.get('ch'), 0) or 0
            if cx is not None and cy is not None:
                if rec_cw > 0 and rec_ch > 0:
                    tol_w = max(8, int(rec_cw * 0.03))
                    tol_h = max(8, int(rec_ch * 0.03))
                    if abs(cw - rec_cw) <= tol_w and abs(ch - rec_ch) <= tol_h:
                        return ox + cx, oy + cy
                elif cw > 0 and ch > 0:
                    return ox + cx, oy + cy
            if 'rx' in anchor and 'ry' in anchor:
                return ox + int(anchor['rx'] * cw), oy + int(anchor['ry'] * ch)
            if cx is not None and cy is not None:
                return ox + cx, oy + cy
            return None, None

        def _move_to_anchor(anchor):
            sx, sy = _screen_from_anchor(anchor)
            if sx is None or sy is None:
                return None, None
            self.smooth_move(sx, sy, precise=True)
            return sx, sy

        for i, a in enumerate(actions):
            if not self.running: break
            t = a.get('type', '')
            ox, oy, cw, ch = _client_pos()

            if t == 'ui_click':
                last_anchor = _extract_anchor(a)
                sx, sy = _move_to_anchor(last_anchor)
                if sx is None:
                    self.log("[MACRO] 좌표 없음")
                    continue
                if not self._interruptible_sleep(random.uniform(0.03, 0.1)): break
                ct = a.get('click', 'click')
                if ct == 'dblclick':
                    self.input("CLICK")
                    if not self._interruptible_sleep(random.uniform(0.08, 0.15)): break
                    self.input("CLICK")
                elif ct == 'rclick':
                    self.input("RCLICK")
                else:
                    self.input("CLICK")
                if not self._interruptible_sleep(random.uniform(0.15, 0.35)): break

            elif t == 'npc_click':
                # A* 이동 → 엔티티 스캔 → NPC 클릭
                nx = _coerce_int(a.get('x'), 0) or 0
                ny = _coerce_int(a.get('y'), 0) or 0
                tid = _coerce_int(a.get('tid'), 0) or 0
                npc_name = a.get('name', '')
                step_anchor = _extract_anchor(a)
                if step_anchor:
                    last_anchor = step_anchor
                px, py = self.pf.read_player()
                if px and nx:
                    d = max(abs(nx-px), abs(ny-py))
                    if d > 3:
                        self.log(f"[SHOP] NPC로 이동 ({nx},{ny})")
                        path, _, _ = self.pf.find_path(px, py, nx, ny)

                        wps = PathFinder.simplify_path(path, 7)
                        npc_walk_start = time.monotonic()
                        for wi in range(1, len(wps)):
                            if not self.running: break
                            if time.monotonic() - npc_walk_start > 15:
                                self.log("[SHOP] NPC 이동 타임아웃"); break
                            self.click_tile(wps[wi][0], wps[wi][1])
                            for _ in range(30):
                                if not self._interruptible_sleep(0.1): break
                                cx, cy = self.pf.read_player()
                                if cx and max(abs(cx-wps[wi][0]), abs(cy-wps[wi][1])) <= 2:
                                    break
                        if not self._interruptible_sleep(0.3): break
                    # 엔티티 스캔으로 NPC 클릭
                    ents = self.pf.discover_entities(full_scan=False)
                    # NPC가 없으면 full_scan으로 재시도
                    if not any(et in ('N', 'D') for et, *_ in ents):
                        ents = self.pf.discover_entities(full_scan=True)
                    px2, py2 = self.pf.read_player()
                    target_sx, target_sy = _screen_from_anchor(step_anchor) if step_anchor else (None, None)
                    best = None
                    best_d = 999
                    best_score = float('inf')
                    for et, ex, ey, *rest in ents:
                        if et not in ('N', 'D'): continue
                        d = max(abs(ex-px2), abs(ey-py2))
                        if d > 15: continue
                        etid = rest[0] if len(rest) > 0 else 0
                        ename = rest[1] if len(rest) > 1 else ''
                        if tid and etid != tid: continue
                        if npc_name and npc_name not in ename: continue
                        if target_sx is not None and target_sy is not None:
                            sx, sy = self.pf.world_to_screen(self.hwnd, px2, py2, ex, ey)
                            score = ((sx - target_sx) ** 2 + (sy - target_sy) ** 2) ** 0.5
                        else:
                            score = float(d)
                        if score < best_score or (score == best_score and d < best_d):
                            best_score = score
                            best_d = d
                            best = (ex, ey)
                    if not best and target_sx is not None and target_sy is not None:
                        for et, ex, ey, *rest in ents:
                            if et not in ('N', 'D'): continue
                            d = max(abs(ex-px2), abs(ey-py2))
                            if d > 15: continue
                            sx, sy = self.pf.world_to_screen(self.hwnd, px2, py2, ex, ey)
                            score = ((sx - target_sx) ** 2 + (sy - target_sy) ** 2) ** 0.5
                            if score < best_score or (score == best_score and d < best_d):
                                best_score = score
                                best_d = d
                                best = (ex, ey)
                    if best:
                        self.log(f"[SHOP] NPC 클릭 ({best[0]},{best[1]})")
                        self.click_tile(best[0], best[1], y_off=-15, avoid_entities=False)
                        if not self._interruptible_sleep(1.0): break
                    else:
                        # fallback: 녹화된 비율 좌표로 클릭
                        if step_anchor:
                            sx, sy = _screen_from_anchor(step_anchor)
                            if sx is None or sy is None:
                                continue
                            self.smooth_move(sx, sy)
                            if not self._interruptible_sleep(0.05): break
                            self.input("CLICK")
                            if not self._interruptible_sleep(1.0): break
                        self.log("[SHOP] NPC 스캔 실패 → 좌표 fallback")

            elif t == 'scroll':
                anchor = _extract_anchor(a) or last_anchor
                if anchor:
                    sx, sy = _move_to_anchor(anchor)
                    if sx is None or sy is None:
                        continue
                    last_anchor = anchor
                    if not self._interruptible_sleep(random.uniform(0.03, 0.08)): break
                delta = _coerce_int(a.get('delta'), -1) or 0
                wheel_gain = max(1, _coerce_int(a.get('wheel_gain'), 1) or 1)
                remaining = abs(delta) * wheel_gain
                direction = -1 if delta < 0 else 1
                interrupted = False
                while remaining > 0:
                    self.input(f"SCROLL:{direction}")
                    remaining -= 1
                    if remaining > 0 and not self._interruptible_sleep(random.uniform(0.05, 0.1)):
                        interrupted = True
                        break
                if interrupted:
                    break
                if delta and not self._interruptible_sleep(random.uniform(0.15, 0.35)): break

            elif t == 'key':
                name = a.get('name', '')
                if name.startswith('F') and name[1:].isdigit():
                    self.input(f"KEY:{name}")
                elif name in ('TAB', 'ESC', 'ENTER'):
                    self.input(f"KEY:{name}")
                elif len(name) == 1:
                    self.input(f"KEY:{name}")
                if not self._interruptible_sleep(random.uniform(0.05, 0.15)): break

            elif t == 'qty_input':
                # 수량 입력: 기본값 ± 랜덤
                base_qty = _coerce_int(a.get('qty'), 100) or 100
                qty_rand = max(0, _coerce_int(a.get('qty_rand'), 0) or 0)
                actual_qty = base_qty + random.randint(-qty_rand, qty_rand)
                actual_qty = max(1, actual_qty)
                qty_str = str(actual_qty)
                self.input("KEY:CTRL_A")  # 전체선택 (없으면 무시)
                if not self._interruptible_sleep(0.05): break
                for digit in qty_str:
                    self.input(f"KEY:{digit}")
                    if not self._interruptible_sleep(random.uniform(0.03, 0.08)): break
                if not self._interruptible_sleep(0.1): break

            elif t == 'wait':
                sec = _coerce_float(a.get('sec'), 1.0) or 0.0
                if not self._interruptible_sleep(sec): break

            elif t == 'walk':
                wx = _coerce_int(a.get('x'), 0) or 0
                wy = _coerce_int(a.get('y'), 0) or 0
                px, py = self.pf.read_player()
                if px and wx:
                    self.log(f"[SHOP] 이동 ({wx},{wy})")
                    walk_start = time.monotonic()
                    while self.running:
                        cx, cy = self.pf.read_player()
                        if not cx:
                            self._interruptible_sleep(0.5)
                            continue
                        if max(abs(cx-wx), abs(cy-wy)) <= 2:
                            self.log(f"[SHOP] 도달 ({cx},{cy})")
                            break
                        if time.monotonic() - walk_start > 30:
                            self.log("[SHOP] walk 타임아웃 (30초)")
                            break

                        path, _, _ = self.pf.find_path(cx, cy, wx, wy)
                        if path and len(path) > 1:
                            wps = PathFinder.simplify_path(path, 7)
                            for wi in range(1, len(wps)):
                                if not self.running: break
                                self.click_tile(wps[wi][0], wps[wi][1])
                                for _ in range(30):
                                    if not self.running: break
                                    self._interruptible_sleep(0.1)
                                    cp = self.pf.read_player()
                                    if cp and max(abs(cp[0]-wps[wi][0]), abs(cp[1]-wps[wi][1])) <= 2:
                                        break
                        else:
                            self._greedy_step(cx, cy, wx, wy)
                            self._interruptible_sleep(0.8)

            self.log(f"[MACRO] {i+1}/{len(actions)} {t}")

    def _interruptible_sleep(self, seconds):
        """self.running 체크하며 sleep — 즉시 정지 가능"""
        while seconds > 0 and self.running:
            delay = min(0.05, seconds)
            time.sleep(delay)
            seconds -= delay
        return self.running

    def _greedy_step(self, cx, cy, tx, ty):
        """A* 실패 시 목적지 방향으로 1칸 이동 (벽 회피)"""
        # 8방향 중 목적지에 가장 가까운 갈 수 있는 타일 선택
        dirs = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
        best_dist = max(abs(cx-tx), abs(cy-ty))
        best = None
        for dx, dy in dirs:
            nx, ny = cx+dx, cy+dy
            dist = max(abs(nx-tx), abs(ny-ty))
            if dist < best_dist:
                best_dist = dist
                best = (nx, ny)
        if best:
            self.log(f"[SHOP] greedy ({cx},{cy})→({best[0]},{best[1]})")
            self.click_tile(best[0], best[1])

    def init_cursor(self):
        """일반 커서 캡처"""
        rect = wt.RECT(); user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        pt = wt.POINT(0, 0); user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        user32.SetCursorPos(pt.x + rect.right // 2, pt.y + rect.bottom // 2 + 50)
        time.sleep(0.1)
        self.normal_cursor = get_cursor_handle()
