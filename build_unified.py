#!/usr/bin/env python3
"""
인터파크 통합 매크로 - Windows 빌드 스크립트
j-2 (Windows)에서 실행하여 .exe 생성

빌드 결과:
  - dist/InterparkMacro.exe (하나의 파일)
"""

import subprocess
import sys
from pathlib import Path


def build_exe():
    project_dir = Path("C:/lmacro_deploy/interpark_macro")
    main_script = project_dir / "interpark_unified.py"
    
    cmd = [
        "pyinstaller",
        "--onefile",
        "--console",
        "--name", "InterparkMacro",
        "--add-data", f"{project_dir}/macro_config.json;.",
        "--hidden-import", "websockets",
        "--hidden-import", "aiohttp",
        "--hidden-import", "asyncio",
        "--hidden-import", "json",
        "--hidden-import", "time",
        "--hidden-import", "logging",
        "--hidden-import", "subprocess",
        "--hidden-import", "datetime",
        "--hidden-import", "pathlib",
        "--hidden-import", "typing",
        "--hidden-import", "dataclasses",
        "--hidden-import", "threading",
        "--hidden-import", "queue",
        "--hidden-import", "urllib.request",
        "--hidden-import", "base64",
        str(main_script)
    ]
    
    print(f"빌드 중: {main_script}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        print("✅ 빌드 성공!")
        
        dist_dir = Path("dist")
        if dist_dir.exists():
            for f in dist_dir.glob("*.exe"):
                size = f.stat().st_size / (1024*1024)
                print(f"📦 {f.name} ({size:.1f} MB)")
    else:
        print("❌ 빌드 실패")
        print(result.stderr)


if __name__ == "__main__":
    build_exe()
