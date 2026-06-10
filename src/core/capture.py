import pyautogui
import keyboard
import os
import sys
import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except:
    ctypes.windll.user32.SetProcessDPIAware()

IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img")
os.makedirs(IMG_DIR, exist_ok=True)

print("=" * 40)
print("  이미지 캡처 도구")
print("=" * 40)
print()
print("  1) 캡처할 영역 위에 마우스를 올려놓고")
print("  2) C 키 → 마우스 중심 200x60 캡처")
print("  3) W 키 → 마우스 중심 300x80 캡처 (넓게)")
print("  4) F 키 → 전체 화면 캡처")
print("  Q 키 → 종료")
print()

count = 0

while True:
    event = keyboard.read_event(suppress=False)
    if event.event_type != 'down':
        continue

    key = event.name.lower()

    if key == 'q':
        print("\n종료.")
        break

    if key == 'f':
        ss = pyautogui.screenshot()
        name = input("  파일명 (확장자 없이): ").strip()
        if not name:
            count += 1
            name = f"capture_{count}"
        path = os.path.join(IMG_DIR, f"{name}.png")
        ss.save(path)
        print(f"  [OK] 전체화면 → {path}")
        continue

    if key in ('c', 'w'):
        x, y = pyautogui.position()
        if key == 'c':
            w, h = 200, 60
        else:
            w, h = 300, 80

        left = max(0, x - w // 2)
        top = max(0, y - h // 2)
        region = (left, top, w, h)

        ss = pyautogui.screenshot(region=region)
        name = input(f"  파일명 (확장자 없이, 영역:{left},{top} {w}x{h}): ").strip()
        if not name:
            count += 1
            name = f"capture_{count}"
        path = os.path.join(IMG_DIR, f"{name}.png")
        ss.save(path)
        print(f"  [OK] {w}x{h} → {path}")
        continue
