# Lineage Classic Hardware Macro

리니지 클래식 다중클라이언트 하드웨어 매크로.

## 기능
- 이동 (웨이포인트)
- 채팅 (대사 리스트, 무제한/1회 루프, n~m초 랜덤 딜레이)
- 좌클릭/우클릭/더블클릭
- 캐릭터 현재 좌표 읽기
- 다중클라 동시성 (PriorityQueue 직렬화)

## 구조
- `src/hw_macro_gui_v4.py` — Python GUI (tkinter)
- `src/scan_dll.c` — C DLL (프로세스 메모리 스캔, 좌표 읽기)
- `driver/lchide.sys` — 커널 드라이버 (하드웨어 입력)
- `install.bat` — 드라이버 설치
- `run.bat` — 실행

## 요구사항
- Windows 10+
- 관리자 권한
- `bcdedit /set testsigning on` + 재부팅 (드라이버 서명 우회)
- Secure Boot OFF

## 빌드
```bash
# DLL (MSVC)
cl /O2 /LD scan_dll.c /Fe:scan_dll.dll /link psapi.lib user32.lib

# EXE (PyInstaller)
pyinstaller --onefile hw_macro_gui_v4.py
```
