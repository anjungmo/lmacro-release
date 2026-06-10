/*
 * scan_dll.c v2 - Smart entity scanner
 * Strategy: find all entity SLOTS once, then just check each slot per tick
 * Build: cl /O2 /LD scan_dll.c /Fe:scan_dll.dll
 */
#include <windows.h>
#include <tlhelp32.h>
#include <psapi.h>
#include <stdio.h>
#include <string.h>

#define MAX_ENTS 128
#define COORD_OFF 104
#define MAX_SLOTS 2048

typedef struct {
    __int64 addr;
    int x, y, d, type;
    char name[64];
} EntResult;

static HANDLE hProc = NULL;
static SIZE_T rd;
static __int64 s_playerAddr;
static DWORD s_pid = 0;
static __int64 s_vtableA;      /* monster/NPC outer VT */
static __int64 s_vtableB;      /* player outer VT */
static __int64 s_monsterVT;

/* candidate addresses found during scan */
#define MAX_CAND 32
static __int64 candAddrs[MAX_CAND];
static int candHP[MAX_CAND];
static int candCount = 0;

/* entity slot cache - addresses where entities have EVER been found */
static __int64 slots[MAX_SLOTS];
static int slotCount = 0;
static CRITICAL_SECTION slotLock;

typedef struct { void *BA, *AB; DWORD AP; SIZE_T RS; DWORD S, P, T; } MBI;

static int ReadMem(void *addr, void *buf, SIZE_T size) {
    return ReadProcessMemory(hProc, addr, buf, size, &rd);
}

static int IsMonster(__int64 ea) {
    __int64 ptr = 0;
    if (!ReadMem((void*)(ea + 8), &ptr, 8)) return 0;
    if (ptr < 0x10000) return 0;
    __int64 ivt = 0;
    if (!ReadMem((void*)ptr, &ivt, 8)) return 0;
    return ivt == s_monsterVT;
}

static void AddSlot(__int64 addr) {
    EnterCriticalSection(&slotLock);
    /* check duplicate */
    for (int i = 0; i < slotCount; i++)
        if (slots[i] == addr) { LeaveCriticalSection(&slotLock); return; }
    if (slotCount < MAX_SLOTS)
        slots[slotCount++] = addr;
    LeaveCriticalSection(&slotLock);
}

/* Fast check: read vtable at known address, verify coords */
__declspec(dllexport) int __cdecl scan_check_slots(int px, int py, int dist, EntResult *out, int maxOut) {
    int count = 0;
    unsigned char buf[128];  /* read vtable(8) + enough for coords */
    int readSize = COORD_OFF + 8;

    EnterCriticalSection(&slotLock);
    int n = slotCount;
    __int64 addrs[MAX_SLOTS];
    memcpy(addrs, slots, n * sizeof(__int64));
    LeaveCriticalSection(&slotLock);

    for (int i = 0; i < n && count < maxOut; i++) {
        if (!ReadMem((void*)addrs[i], buf, readSize)) continue;
        /* check vtable */
        { unsigned __int64 vt = *(unsigned __int64*)buf;
          if (vt != s_vtableA && vt != s_vtableB) continue; }
        /* check coords */
        int vx = *(int*)(buf + COORD_OFF);
        int vy = *(int*)(buf + COORD_OFF + 4);
        if (vx <= 30000 || vx >= 40000 || vy <= 30000 || vy >= 40000) continue;
        int dx = abs(vx - px), dy = abs(vy - py);
        if (dx > dist || dy > dist) continue;
        if (dx <= 1 && dy <= 1) continue;  /* skip self */
        /* check monster type */
        if (!IsMonster(addrs[i])) continue;
        out[count].addr = addrs[i];
        out[count].x = vx; out[count].y = vy;
        out[count].d = dx + dy;
        /* read mob type + name */
        int mobId = 0;
        __int64 innerPtr = 0;
        if (ReadMem((void*)(addrs[i] + 8), &innerPtr, 8) && innerPtr > 0x10000)
            ReadMem((void*)(innerPtr + 48), &mobId, 4);
        out[count].type = mobId;
        /* name: deref(+8) -> deref(+24) -> deref(+32) -> +64 */
        out[count].name[0] = 0;
        __int64 p1=0, p2=0, p3=0;
        if (ReadMem((void*)(addrs[i]+8), &p1, 8) && p1>0x10000)
            if (ReadMem((void*)(p1+24), &p2, 8) && p2>0x10000)
                if (ReadMem((void*)(p2+32), &p3, 8) && p3>0x10000)
                    ReadMem((void*)(p3+64), out[count].name, 63);
        out[count].name[63] = 0;
        count++;
    }
    return count;
}

/* Full page scan to discover new entity slots (both VTs) */
static int DiscoverSlots(int px, int py) {
    unsigned __int64 vt1 = s_vtableA;  /* monster/NPC VT */
    unsigned __int64 vt2 = s_vtableB;  /* player VT */
    if (!vt1 && !vt2) return 0;  /* no vtables yet */
    int newFound = 0;
    MBI mbi;
    __int64 a = 0x10000;
    while (a < 0x7FFFFFFFFFFF) {
        if (!VirtualQueryEx(hProc, (void*)a, (MEMORY_BASIC_INFORMATION*)&mbi, sizeof(mbi))) break;
        if (mbi.S == MEM_COMMIT && mbi.T == MEM_PRIVATE &&
            (mbi.P & 0xEE) && mbi.RS >= 0x1000 && mbi.RS < 0x4000000) {
            SIZE_T regionSize = mbi.RS;
            unsigned char *data = (unsigned char*)VirtualAlloc(NULL, regionSize, MEM_COMMIT, PAGE_READWRITE);
            if (data && ReadMem(mbi.BA, data, regionSize)) {
                unsigned __int64 *p = (unsigned __int64*)data;
                unsigned __int64 *end = (unsigned __int64*)(data + regionSize - 7);
                for (; p < end; p++) {
                    if (*p != vt1 && *p != vt2) continue;
                    SIZE_T idx = (unsigned char*)p - data;
                    if (idx + COORD_OFF + 8 > regionSize) continue;
                    int vx = *(int*)(data + idx + COORD_OFF);
                    int vy = *(int*)(data + idx + COORD_OFF + 4);
                    if (vx > 30000 && vx < 40000 && vy > 30000 && vy < 40000) {
                        __int64 ea = (__int64)mbi.BA + idx;
                        AddSlot(ea);
                        newFound++;
                    }
                }
            }
            if (data) VirtualFree(data, 0, MEM_RELEASE);
        }
        a = (__int64)mbi.BA + mbi.RS;
        if (a <= (__int64)mbi.BA) break;
    }
    return newFound;
}

/* Purge stale slots - remove entries with invalid VT or coords */
static void PurgeStaleSlots(void) {
    unsigned char buf[112];
    EnterCriticalSection(&slotLock);
    int newCount = 0;
    for (int i = 0; i < slotCount; i++) {
        if (!ReadMem((void*)slots[i], buf, COORD_OFF + 8)) continue;
        unsigned __int64 vt = *(unsigned __int64*)buf;
        if (vt != s_vtableA && vt != s_vtableB) continue;
        int vx = *(int*)(buf + COORD_OFF);
        int vy = *(int*)(buf + COORD_OFF + 4);
        if (vx <= 30000 || vx >= 40000 || vy <= 30000 || vy >= 40000) continue;
        slots[newCount++] = slots[i];
    }
    slotCount = newCount;
    LeaveCriticalSection(&slotLock);
}

/* Background discovery thread */
static volatile int bgRunning = 0;
static volatile int bgPx = 0, bgPy = 0;
static volatile int bgNewSlots = 0;

static DWORD WINAPI BgDiscoverThread(LPVOID param) {
    (void)param;
    int cycle = 0;
    while (bgRunning) {
        if (bgPx > 0) {
            /* Every tick: purge stale slots */
            PurgeStaleSlots();
            /* Every 3 ticks: discover new slots */
            if (cycle % 3 == 0) {
                int n = DiscoverSlots(bgPx, bgPy);
                if (n > 0) bgNewSlots += n;
            }
            cycle++;
        }
        Sleep(300);
    }
    return 0;
}

/* ===== Exported functions ===== */

__declspec(dllexport) int __cdecl scan_init(int maxHP, int maxMP) {
    InitializeCriticalSection(&slotLock);

    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    PROCESSENTRY32 pe = { sizeof(pe) };
    DWORD pid = 0;
    /* First: find by window title containing "Lineage" */
    WCHAR title[256];
    HWND h = GetWindow(GetDesktopWindow(), GW_CHILD);
    while (h) {
        if (GetWindowTextW(h, title, 256) > 0) {
            if (wcsstr(title, L"Lineage") != NULL) {
                DWORD wpid = 0;
                GetWindowThreadProcessId(h, &wpid);
                if (wpid) { pid = wpid; break; }
            }
        }
        h = GetWindow(h, GW_HWNDNEXT);
    }
    /* Fallback: try known process base names (with or without .exe) */
    if (!pid && Process32First(snap, &pe)) {
        do {
            char lower[MAX_PATH];
            int len = strlen(pe.szExeFile);
            for (int i=0; i<len; i++) lower[i] = tolower(pe.szExeFile[i]);
            lower[len] = 0;
            /* Strip .exe suffix for comparison */
            char base[MAX_PATH];
            strcpy(base, lower);
            char *dot = strstr(base, ".exe");
            if (dot) *dot = 0;
            if (strcmp(base,"lc")==0 || strcmp(base,"sv")==0 || strcmp(base,"lineage")==0)
                { pid = pe.th32ProcessID; goto found_proc; }
        } while (Process32Next(snap, &pe));
    }
    found_proc:
    CloseHandle(snap);
    if (!pid) return -1;  /* no lineage process */
    s_pid = pid;

    hProc = OpenProcess(PROCESS_ALL_ACCESS, FALSE, pid);
    if (!hProc) return -2;  /* no admin rights */

    printf("[scan] pid=%lu hProc=%p\r\n", pid, hProc);

    /* Get exe base via EnumProcessModulesEx */
    HMODULE exeMod = 0;
    DWORD cbNeeded = 0;
    EnumProcessModulesEx(hProc, &exeMod, sizeof(exeMod), &cbNeeded, LIST_MODULES_ALL);
    __int64 exeBase = (__int64)exeMod;

    /* VTs: initially 0, auto-learned after finding player */
    s_vtableA = 0;
    s_vtableB = 0;
    s_monsterVT = 0;

    /* Phase 1: find player without vtable (HP/MP/coord/direction heuristic) */
    int autoMode = (maxHP == 0);
    __int64 bestAddr = 0; int bestHP = 0;  /* autoMode: pick highest mHP candidate */
    candCount = 0;
    int matchCount = 0, regionCount = 0, readOk = 0;
    MBI mbi;
    __int64 a = 0x10000;
    while (a < 0x7FFFFFFFFFFF) {
        if (!VirtualQueryEx(hProc, (void*)a, (MEMORY_BASIC_INFORMATION*)&mbi, sizeof(mbi))) break;
        if (mbi.S == MEM_COMMIT && (mbi.P & 0xEE) && mbi.RS >= 0x1000 && mbi.RS < 0x10000000) {
            regionCount++;
            SIZE_T regionSize = mbi.RS;
            unsigned char *data = (unsigned char*)VirtualAlloc(NULL, regionSize, MEM_COMMIT, PAGE_READWRITE);
            BOOL rd = ReadMem(mbi.BA, data, regionSize);
            if (rd) readOk++;
            if (data && rd) {
                for (SIZE_T i = 16; i + 28 < regionSize; i += 4) {
                    int vx = *(int*)(data+i);
                    int vy = *(int*)(data+i+4);
                    if (vx < 25000 || vx > 50000 || vy < 25000 || vy > 50000) continue;
                    int dir  = *(int*)(data+i+8);
                    int cHP  = *(int*)(data+i+12);
                    int mHP  = *(int*)(data+i+16);
                    int cMP  = *(int*)(data+i+20);
                    int mMP  = *(int*)(data+i+24);
                    if (autoMode) {
                        if (dir < 0 || dir > 7) continue;
                        if (cHP <= 0 || mHP < 5 || cHP > mHP || mHP > 50000) continue;
                        if (cMP < 0 || mMP < 1 || cMP > mMP || mMP > 50000) continue;
                        /* Extra validation: nearby memory should look sane */
                        if (i + 36 < regionSize) {
                            int vx2 = *(int*)(data+i+28);
                            int vy2 = *(int*)(data+i+32);
                            if (vx2 > 30000 && vx2 < 40000 && vy2 > 30000 && vy2 < 40000) continue;
                        }
                        /* Keep best candidate (highest mHP) */
                        if (mHP > bestHP) {
                            matchCount++;
                            bestHP = mHP;
                            bestAddr = (__int64)mbi.BA + i;
                        }
                        /* Store all candidates */
                        if (candCount < MAX_CAND) {
                            candAddrs[candCount] = (__int64)mbi.BA + i;
                            candHP[candCount] = mHP;
                            candCount++;
                        }
                        continue;  /* don't take first match, keep scanning */
                    } else {
                        if (mHP != maxHP || mMP != maxMP) continue;
                        if (cHP < 0 || cHP > maxHP) continue;
                    }

                    s_playerAddr = (__int64)mbi.BA + i;
                    VirtualFree(data, 0, MEM_RELEASE);
                    goto found;
                }
            }
            if (data) VirtualFree(data, 0, MEM_RELEASE);
        }
        a = (__int64)mbi.BA + mbi.RS;
        if (a <= (__int64)mbi.BA) break;
    }
    printf("[scan] regions=%d readOk=%d matches=%d bestHP=%d bestAddr=0x%llX\r\n",
           regionCount, readOk, matchCount, bestHP, bestAddr);
    if (autoMode && bestAddr) {
        s_playerAddr = bestAddr;
        printf("[scan] using bestAddr=0x%llX bestHP=%d (verify skipped)\r\n", bestAddr, bestHP);
        goto found;
    }
    return 0;
found:

    /* initial discovery */
    DiscoverSlots(0, 0);

    /* start background discovery */
    bgRunning = 1;
    CreateThread(NULL, 0, BgDiscoverThread, NULL, 0, NULL);

    return 1;
}

/* Debug: dump hProc after init */
__declspec(dllexport) void* __cdecl scan_get_hproc(void) { return hProc; }

/* Rescan: re-run Phase 1 scan to find player */
__declspec(dllexport) int __cdecl scan_rescan(void) {
    if (!s_pid) return 0;
    printf("[rescan] starting for pid=%lu\r\n", s_pid);
    /* Re-open handle if needed */
    if (!hProc) hProc = OpenProcess(PROCESS_ALL_ACCESS, FALSE, s_pid);
    if (!hProc) { printf("[rescan] OpenProcess failed err=%lu\r\n", GetLastError()); return 0; }
    s_playerAddr = 0;
    int autoMode = 1;
    __int64 bestAddr = 0; int bestHP = 0, matchCount = 0, regionCount = 0, readOk = 0;
    MBI mbi; __int64 a = 0x10000;
    while (a < 0x7FFFFFFFFFFF) {
        if (!VirtualQueryEx(hProc, (void*)a, (PMEMORY_BASIC_INFORMATION)&mbi, sizeof(mbi))) break;
        if (mbi.S == MEM_COMMIT && (mbi.P == PAGE_READWRITE)) {
            regionCount++;
            SIZE_T regionSize = mbi.RS;
            unsigned char *data = (unsigned char*)VirtualAlloc(NULL, regionSize, MEM_COMMIT, PAGE_READWRITE);
            BOOL rd = ReadMem(mbi.BA, data, regionSize);
            if (rd) readOk++;
            if (data && rd) {
                for (SIZE_T i = 16; i + 28 < regionSize; i += 4) {
                    int vx = *(int*)(data+i);
                    int vy = *(int*)(data+i+4);
                    if (vx < 25000 || vx > 50000 || vy < 25000 || vy > 50000) continue;
                    int dir  = *(int*)(data+i+8);
                    int cHP  = *(int*)(data+i+12);
                    int mHP  = *(int*)(data+i+16);
                    int cMP  = *(int*)(data+i+20);
                    int mMP  = *(int*)(data+i+24);
                    if (dir < 0 || dir > 7) continue;
                    if (cHP <= 0 || mHP < 5 || cHP > mHP || mHP > 50000) continue;
                    if (cMP < 0 || mMP < 1 || cMP > mMP || mMP > 50000) continue;
                    if (i + 36 < regionSize) {
                        int vx2 = *(int*)(data+i+28);
                        int vy2 = *(int*)(data+i+32);
                        if (vx2 > 25000 && vx2 < 50000 && vy2 > 25000 && vy2 < 50000) continue;
                    }
                    if (mHP > bestHP) {
                        matchCount++;
                        bestHP = mHP;
                        bestAddr = (__int64)mbi.BA + i;
                    }
                }
            }
            if (data) VirtualFree(data, 0, MEM_RELEASE);
        }
        a = (__int64)mbi.BA + mbi.RS;
        if (a <= (__int64)mbi.BA) break;
    }
    printf("[rescan] regions=%d readOk=%d matches=%d bestHP=%d bestAddr=0x%llX\r\n",
           regionCount, readOk, matchCount, bestHP, bestAddr);
    if (bestAddr) {
        s_playerAddr = bestAddr;
        return 1;
    }
    return 0;
}

__declspec(dllexport) __int64 __cdecl scan_get_player_addr(void) { return s_playerAddr; }
__declspec(dllexport) DWORD __cdecl scan_get_pid(void) { return s_pid; }

/* Candidate enumeration */
__declspec(dllexport) int __cdecl scan_get_cand_count(void) { return candCount; }
__declspec(dllexport) __int64 __cdecl scan_get_cand_addr(int idx) {
    return (idx >= 0 && idx < candCount) ? candAddrs[idx] : 0;
}
__declspec(dllexport) int __cdecl scan_get_cand_hp(int idx) {
    return (idx >= 0 && idx < candCount) ? candHP[idx] : 0;
}
__declspec(dllexport) void __cdecl scan_set_player_addr(__int64 addr) {
    s_playerAddr = addr;
}
__declspec(dllexport) __int64 __cdecl scan_get_vtable(void) { return s_vtableA; }
__declspec(dllexport) int __cdecl scan_get_slot_count(void) { return slotCount; }
__declspec(dllexport) int __cdecl scan_get_new_slots(void) { int n = bgNewSlots; bgNewSlots = 0; return n; }

__declspec(dllexport) int __cdecl scan_read_player(int *outX, int *outY) {
    if (!s_playerAddr || !s_pid) { printf("[rp] s_playerAddr=0 or s_pid=0\r\n"); return 0; }
    /* Open FRESH handle each time — cached hProc goes stale */
    HANDLE h = OpenProcess(PROCESS_ALL_ACCESS, FALSE, s_pid);
    if (!h) { printf("[rp] OpenProcess failed err=%lu pid=%lu\r\n", GetLastError(), s_pid); return 0; }
    int c[2];
    SIZE_T rd2;
    int ok = ReadProcessMemory(h, (void*)s_playerAddr, c, 8, &rd2);
    DWORD err = ok ? 0 : GetLastError();
    printf("[rp] fresh h=%p ok=%d err=%lu x=%d y=%d\r\n", h, ok, err, c[0], c[1]);
    CloseHandle(h);
    if (!ok || c[0] < 10000 || c[0] > 60000 || c[1] < 10000 || c[1] > 60000) return 0;
    printf("[rp] raw: x=%d y=%d\r\n", c[0], c[1]);
    *outX = c[0]; *outY = c[1]; bgPx = c[0]; bgPy = c[1]; return 1;
}

/* Debug: return exe base for diagnosis */
__declspec(dllexport) int __cdecl scan_get_exe_base(void) {
    if (!hProc) return 0;
    HMODULE exeMod = 0; DWORD cb = 0;
    EnumProcessModulesEx(hProc, &exeMod, sizeof(exeMod), &cb, LIST_MODULES_ALL);
    return (int)(__int64)exeMod;
}

__declspec(dllexport) int __cdecl scan_read_hp(int *chp, int *mhp, int *cmp, int *mmp) {
    int buf[4];
    if (ReadMem((void*)(s_playerAddr + 12), buf, 16)) {
        *chp = buf[0]; *mhp = buf[1]; *cmp = buf[2]; *mmp = buf[3]; return 1;
    }
    return 0;
}

__declspec(dllexport) int __cdecl scan_read_state(int *state, int *action) {
    int buf[2];
    if (ReadMem((void*)(s_playerAddr + 32), buf, 8)) {
        *state = buf[0]; *action = buf[1]; return 1;
    }
    return 0;
}

/* Export raw slot addresses for Python classification */
__declspec(dllexport) int __cdecl scan_get_slots(__int64 *outAddrs, int maxOut) {
    EnterCriticalSection(&slotLock);
    int n = slotCount < maxOut ? slotCount : maxOut;
    memcpy(outAddrs, slots, n * sizeof(__int64));
    LeaveCriticalSection(&slotLock);
    return n;
}

/* Force a full discovery now (non-blocking result via slots) */
__declspec(dllexport) int __cdecl scan_discover_now(void) {
    return DiscoverSlots(bgPx, bgPy);
}

/* Main scan: check slots (fast) */
__declspec(dllexport) int __cdecl scan_entities(int px, int py, int dist, EntResult *out, int maxOut) {
    return scan_check_slots(px, py, dist, out, maxOut);
}

__declspec(dllexport) int __cdecl scan_full(int px, int py, int dist, EntResult *out, int maxOut) {
    DiscoverSlots(px, py);
    return scan_check_slots(px, py, dist, out, maxOut);
}

/* scan all entities (no monster filter) - for finding players */
__declspec(dllexport) int __cdecl scan_all(int px, int py, int dist, EntResult *out, int maxOut) {
    int count = 0;
    unsigned char buf[128];
    int readSize = COORD_OFF + 8;

    EnterCriticalSection(&slotLock);
    int n = slotCount;
    __int64 addrs[MAX_SLOTS];
    memcpy(addrs, slots, n * sizeof(__int64));
    LeaveCriticalSection(&slotLock);

    for (int i = 0; i < n && count < maxOut; i++) {
        if (!ReadMem((void*)addrs[i], buf, readSize)) continue;
        { unsigned __int64 vt = *(unsigned __int64*)buf;
          if (vt != s_vtableA && vt != s_vtableB) continue; }
        int vx = *(int*)(buf + COORD_OFF);
        int vy = *(int*)(buf + COORD_OFF + 4);
        if (vx <= 30000 || vx >= 40000 || vy <= 30000 || vy >= 40000) continue;
        int dx = abs(vx - px), dy = abs(vy - py);
        if (dx > dist || dy > dist) continue;
        if (dx <= 1 && dy <= 1) continue;
        out[count].addr = addrs[i];
        out[count].x = vx; out[count].y = vy;
        out[count].d = dx + dy;
        /* type: 1=monster, 0=player/npc */
        out[count].type = IsMonster(addrs[i]) ? 1 : 0;
        out[count].name[0] = 0;
        count++;
    }
    return count;
}

/* ===== TILEMAP + A* PATHFINDING ===== */
/* L1J map 4 (Mainland Aden) from text file */
#define MAP_ORIGIN_X 32448
#define MAP_ORIGIN_Y 32064
#define MAP_WIDTH 1856     /* columns (X range: 32448-34303) */
#define MAP_HEIGHT 1472    /* rows (Y range: 32064-33535) */
#define MAP_TOTAL (MAP_WIDTH * MAP_HEIGHT)

static unsigned char *tileCache = NULL;
static int tileCacheLoaded = 0;
static char mapFilePath[512] = {0};

__declspec(dllexport) void __cdecl map_set_path(const char *path) {
    strncpy(mapFilePath, path, 511);
}

__declspec(dllexport) int __cdecl map_load_tiles(void) {
    if (tileCache) { VirtualFree(tileCache, 0, MEM_RELEASE); tileCache = NULL; }
    tileCache = (unsigned char*)VirtualAlloc(NULL, MAP_TOTAL, MEM_COMMIT, PAGE_READWRITE);
    if (!tileCache) return 0;
    memset(tileCache, 0, MAP_TOTAL);

    /* Load from L1J text map file */
    FILE *f = fopen(mapFilePath[0] ? mapFilePath : "maps/4.txt", "r");
    if (!f) return 0;

    char line[32768];
    int row = 0;
    while (row < MAP_HEIGHT && fgets(line, sizeof(line), f)) {
        int col = 0;
        char *p = line;
        while (*p && col < MAP_WIDTH) {
            /* parse comma-separated integers */
            while (*p == ' ' || *p == '\t') p++;
            if (*p == '\r' || *p == '\n' || *p == '\0') break;
            int val = 0;
            while (*p >= '0' && *p <= '9') { val = val * 10 + (*p - '0'); p++; }
            tileCache[row * MAP_WIDTH + col] = (unsigned char)val;  /* Y-major: [y][x] */
            col++;
            if (*p == ',') p++;
        }
        row++;
    }
    fclose(f);
    tileCacheLoaded = row * MAP_WIDTH;
    return tileCacheLoaded;
}

__declspec(dllexport) int __cdecl map_get_tile(int x, int y) {
    if (!tileCache) return -1;
    int lx = x - MAP_ORIGIN_X;
    int ly = y - MAP_ORIGIN_Y;
    if (lx < 0 || ly < 0 || lx >= MAP_WIDTH || ly >= MAP_HEIGHT) return -1;
    int offset = ly * MAP_WIDTH + lx;  /* Y-major: [y][x] */
    if (offset < 0 || offset >= tileCacheLoaded) return -1;
    return tileCache[offset];
}

/* L1J tile bits: bit0=East move, bit1=North move, bit2=East arrow, bit3=North arrow,
 * bit4=safety zone, bit5=combat zone, bit7=absolute impassable
 * Walkable = has at least one movement bit (0,1) and no bit7 */
static int tile_ok(int t) { return (t & 0x03) && !(t & 0x80); }

__declspec(dllexport) int __cdecl map_is_passable(int x, int y, int heading) {
    if (!tileCache) return 0;
    int tile1 = map_get_tile(x, y);
    if (!tile_ok(tile1)) return 0;
    int nx = x, ny = y;
    if (heading == 0) ny--;
    else if (heading == 1) { nx++; ny--; }
    else if (heading == 2) nx++;
    else if (heading == 3) { nx++; ny++; }
    else if (heading == 4) ny++;
    else if (heading == 5) { nx--; ny++; }
    else if (heading == 6) nx--;
    else if (heading == 7) { nx--; ny--; }
    int tile2 = map_get_tile(nx, ny);
    if (!tile_ok(tile2)) return 0;

    /* L1J edge-based checks:
     * bit0 = East edge passable (between tile and tile+1 in X)
     * bit1 = North edge passable (between tile and tile-1 in Y)
     * Cardinal: check single edge
     * Diagonal: check two L-shaped paths via intermediate tiles */

    /* Cardinal directions */
    if (heading == 0) return (tile1 & 0x02) != 0;           /* N: source's N edge */
    if (heading == 2) return (tile1 & 0x01) != 0;           /* E: source's E edge */
    if (heading == 4) return (tile2 & 0x02) != 0;           /* S: dest's N edge (shared) */
    if (heading == 6) return (tile2 & 0x01) != 0;           /* W: dest's E edge (shared) */

    /* Diagonal: need at least one L-shaped path fully passable */
    /* NE (1): to (x+1, y-1) */
    if (heading == 1) {
        int tN = map_get_tile(x, y-1);
        int tE = map_get_tile(x+1, y);
        /* Path A: N then E — canN(x,y) && intermediate_ok && canE(x,y-1) */
        int pA = (tile1 & 0x02) && tile_ok(tN) && (tN & 0x01);
        /* Path B: E then N — canE(x,y) && intermediate_ok && canN(x+1,y) */
        int pB = (tile1 & 0x01) && tile_ok(tE) && (tE & 0x02);
        return pA || pB;
    }
    /* SE (3): to (x+1, y+1) */
    if (heading == 3) {
        int tS = map_get_tile(x, y+1);
        int tE = map_get_tile(x+1, y);
        /* Path A: S then E — canS(x,y)=tS.bit1 && intermediate_ok && canE(x,y+1)=tS.bit0 */
        int pA = tile_ok(tS) && (tS & 0x02) && (tS & 0x01);
        /* Path B: E then S — canE(x,y)=tile1.bit0 && intermediate_ok && canS(x+1,y)=tile2.bit1 */
        int pB = (tile1 & 0x01) && tile_ok(tE) && (tile2 & 0x02);
        return pA || pB;
    }
    /* SW (5): to (x-1, y+1) */
    if (heading == 5) {
        int tS = map_get_tile(x, y+1);
        int tW = map_get_tile(x-1, y);
        /* Path A: S then W — canS(x,y)=tS.bit1 && intermediate_ok && canW(x,y+1)=tile2.bit0 */
        int pA = tile_ok(tS) && (tS & 0x02) && (tile2 & 0x01);
        /* Path B: W then S — canW(x,y)=tW.bit0 && intermediate_ok && canS(x-1,y)=tile2.bit1 */
        int pB = tile_ok(tW) && (tW & 0x01) && (tile2 & 0x02);
        return pA || pB;
    }
    /* NW (7): to (x-1, y-1) */
    if (heading == 7) {
        int tN = map_get_tile(x, y-1);
        int tW = map_get_tile(x-1, y);
        /* Path A: N then W — canN(x,y)=tile1.bit1 && intermediate_ok && canW(x,y-1)=tile2.bit0 */
        int pA = (tile1 & 0x02) && tile_ok(tN) && (tile2 & 0x01);
        /* Path B: W then N — canW(x,y)=tW.bit0 && intermediate_ok && canN(x-1,y)=tW.bit1 */
        int pB = tile_ok(tW) && (tW & 0x01) && (tW & 0x02);
        return pA || pB;
    }
    return 0;
}

/* ===== A* PATHFINDING (Binary Heap + Hash Map) ===== */
#define ASTAR_MAX 262144
#define HASH_SIZE 524288   /* must be power of 2, >= 2*ASTAR_MAX */
#define HASH_MASK (HASH_SIZE - 1)

typedef struct { int x, y, g, f, parent; char closed; } ANode;
typedef struct { int x, y; } PathPoint;

/* all A* state is heap-allocated per call to avoid huge static arrays */
static ANode *aNodes = NULL;
static int *heap = NULL;       /* min-heap of node indices, sorted by f */
static int heapSize;
static int *hashTable = NULL;  /* coordinate -> node index, -1 = empty */
static int totalNodes;

static int astar_heuristic(int x1, int y1, int x2, int y2) {
    int dx = abs(x1-x2), dy = abs(y1-y2);
    /* octile distance * 10 */
    return (dx > dy) ? dx * 10 + dy * 4 : dy * 10 + dx * 4;
}

static unsigned int coord_hash(int x, int y) {
    return (unsigned int)((x * 73856093u) ^ (y * 19349663u)) & HASH_MASK;
}

static int hash_find(int x, int y) {
    unsigned int h = coord_hash(x, y);
    for (int i = 0; i < HASH_SIZE; i++) {
        unsigned int idx = (h + i) & HASH_MASK;
        int ni = hashTable[idx];
        if (ni == -1) return -1;
        if (aNodes[ni].x == x && aNodes[ni].y == y) return ni;
    }
    return -1;
}

static void hash_insert(int x, int y, int nodeIdx) {
    unsigned int h = coord_hash(x, y);
    for (int i = 0; i < HASH_SIZE; i++) {
        unsigned int idx = (h + i) & HASH_MASK;
        if (hashTable[idx] == -1) { hashTable[idx] = nodeIdx; return; }
    }
}

/* Binary min-heap by f-value */
static void heap_swap(int a, int b) {
    int t = heap[a]; heap[a] = heap[b]; heap[b] = t;
}

static void heap_up(int i) {
    while (i > 0) {
        int p = (i - 1) / 2;
        if (aNodes[heap[i]].f < aNodes[heap[p]].f) { heap_swap(i, p); i = p; }
        else break;
    }
}

static void heap_down(int i) {
    while (1) {
        int best = i, l = 2*i+1, r = 2*i+2;
        if (l < heapSize && aNodes[heap[l]].f < aNodes[heap[best]].f) best = l;
        if (r < heapSize && aNodes[heap[r]].f < aNodes[heap[best]].f) best = r;
        if (best == i) break;
        heap_swap(i, best); i = best;
    }
}

static void heap_push(int nodeIdx) {
    heap[heapSize] = nodeIdx;
    heap_up(heapSize);
    heapSize++;
}

static int heap_pop(void) {
    int top = heap[0];
    heap[0] = heap[--heapSize];
    if (heapSize > 0) heap_down(0);
    return top;
}

/* re-heapify after g update - find and bubble up */
static void heap_decrease(int nodeIdx) {
    for (int i = 0; i < heapSize; i++) {
        if (heap[i] == nodeIdx) { heap_up(i); return; }
    }
}

__declspec(dllexport) int __cdecl map_find_path(int sx, int sy, int dx, int dy,
                                                 PathPoint *outPath, int maxPath) {
    if (!tileCache) return 0;

    /* allocate on first call */
    if (!aNodes) {
        aNodes = (ANode*)VirtualAlloc(NULL, ASTAR_MAX * sizeof(ANode), MEM_COMMIT, PAGE_READWRITE);
        heap = (int*)VirtualAlloc(NULL, ASTAR_MAX * sizeof(int), MEM_COMMIT, PAGE_READWRITE);
        hashTable = (int*)VirtualAlloc(NULL, HASH_SIZE * sizeof(int), MEM_COMMIT, PAGE_READWRITE);
        if (!aNodes || !heap || !hashTable) return 0;
    }

    /* clear hash table */
    memset(hashTable, 0xFF, HASH_SIZE * sizeof(int));  /* fill with -1 */
    totalNodes = 0;
    heapSize = 0;

    /* start node */
    aNodes[0].x = sx; aNodes[0].y = sy;
    aNodes[0].g = 0;
    aNodes[0].f = astar_heuristic(sx, sy, dx, dy);
    aNodes[0].parent = -1;
    aNodes[0].closed = 0;
    totalNodes = 1;
    hash_insert(sx, sy, 0);
    heap_push(0);

    static const int ddx[] = {0,1,1,1,0,-1,-1,-1};
    static const int ddy[] = {-1,-1,0,1,1,1,0,-1};
    static const int cost[] = {10,14,10,14,10,14,10,14};

    while (heapSize > 0 && totalNodes < ASTAR_MAX - 8) {
        int cur = heap_pop();
        ANode *cn = &aNodes[cur];
        if (cn->closed) continue;
        cn->closed = 1;

        /* reached destination? */
        if (cn->x == dx && cn->y == dy) {
            /* trace back using parent index */
            int pathLen = 0;
            int ni = cur;
            PathPoint *temp = (PathPoint*)VirtualAlloc(NULL, ASTAR_MAX * sizeof(PathPoint), MEM_COMMIT, PAGE_READWRITE);
            if (!temp) return 0;
            while (ni >= 0 && pathLen < ASTAR_MAX) {
                temp[pathLen].x = aNodes[ni].x;
                temp[pathLen].y = aNodes[ni].y;
                ni = aNodes[ni].parent;
                pathLen++;
            }
            /* reverse into output */
            int outLen = 0;
            for (int i = pathLen - 1; i >= 0 && outLen < maxPath; i--)
                outPath[outLen++] = temp[i];
            VirtualFree(temp, 0, MEM_RELEASE);
            return outLen;
        }

        /* expand 8 neighbors */
        for (int d = 0; d < 8; d++) {
            if (!map_is_passable(cn->x, cn->y, d)) continue;
            int nx = cn->x + ddx[d];
            int ny = cn->y + ddy[d];
            int ng = cn->g + cost[d];
            int existing = hash_find(nx, ny);
            if (existing >= 0) {
                if (aNodes[existing].closed) continue;
                if (ng < aNodes[existing].g) {
                    aNodes[existing].g = ng;
                    aNodes[existing].f = ng + astar_heuristic(nx, ny, dx, dy);
                    aNodes[existing].parent = cur;
                    heap_decrease(existing);
                }
            } else {
                int ni = totalNodes++;
                aNodes[ni].x = nx; aNodes[ni].y = ny;
                aNodes[ni].g = ng;
                aNodes[ni].f = ng + astar_heuristic(nx, ny, dx, dy);
                aNodes[ni].parent = cur;
                aNodes[ni].closed = 0;
                hash_insert(nx, ny, ni);
                heap_push(ni);
            }
        }
    }

    /* No full path found — return partial path to closest reachable node */
    {
        int bestNode = -1, bestH = 0x7FFFFFFF;
        for (int i = 0; i < totalNodes; i++) {
            if (!aNodes[i].closed) continue;
            int h = astar_heuristic(aNodes[i].x, aNodes[i].y, dx, dy);
            if (h < bestH) { bestH = h; bestNode = i; }
        }
        if (bestNode >= 0 && bestNode != 0) {  /* 0 = start node, skip if we didn't move */
            int pathLen = 0;
            int ni = bestNode;
            PathPoint *temp = (PathPoint*)VirtualAlloc(NULL, ASTAR_MAX * sizeof(PathPoint), MEM_COMMIT, PAGE_READWRITE);
            if (!temp) return 0;
            while (ni >= 0 && pathLen < ASTAR_MAX) {
                temp[pathLen].x = aNodes[ni].x;
                temp[pathLen].y = aNodes[ni].y;
                ni = aNodes[ni].parent;
                pathLen++;
            }
            int outLen = 0;
            for (int i = pathLen - 1; i >= 0 && outLen < maxPath; i--)
                outPath[outLen++] = temp[i];
            VirtualFree(temp, 0, MEM_RELEASE);
            return -outLen;  /* negative = partial path */
        }
    }
    return 0; /* truly no path */
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID r) {
    (void)h; (void)reason; (void)r; return TRUE;
}
