#!/usr/bin/env python3
"""
인터파크 티켓팅 매크로 - Windows 실행 파일 빌드 스크립트
j-2 (Windows)에서 실행하여 .exe 생성
"""

import subprocess
import sys
import os
from pathlib import Path


def build_exe():
    """PyInstaller로 .exe 빌드"""
    
    # 프로젝트 경로
    project_dir = Path("C:/lmacro_deploy/interpark_macro")
    
    # 메인 스크립트
    main_script = project_dir / "interpark_macro_api.py"
    capture_script = project_dir / "api_capture_agent.py"
    
    # 빌드 명령어
    build_cmds = [
        # 메인 매크로
        [
            "pyinstaller",
            "--onefile",
            "--windowed",
            "--name", "InterparkMacro",
            "--add-data", f"{project_dir}/accounts.json.example;.",
            "--add-data", f"{project_dir}/proxies.txt.example;.",
            "--hidden-import", "aiohttp",
            "--hidden-import", "websockets",
            "--hidden-import", "ntplib",
            "--hidden-import", "asyncio",
            "--hidden-import", "json",
            "--hidden-import", "logging",
            "--hidden-import", "subprocess",
            "--hidden-import", "time",
            "--hidden-import", "datetime",
            "--hidden-import", "pathlib",
            "--hidden-import", "typing",
            "--hidden-import", "dataclasses",
            "--hidden-import", "concurrent.futures",
            "--hidden-import", "threading",
            "--hidden-import", "collections",
            "--hidden-import", "hashlib",
            "--hidden-import", "hmac",
            "--hidden-import", "base64",
            "--hidden-import", "urllib.request",
            str(main_script)
        ],
        
        # API 캡처 에이전트
        [
            "pyinstaller",
            "--onefile",
            "--console",
            "--name", "InterparkCapture",
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
            "--hidden-import", "urllib.request",
            str(capture_script)
        ]
    ]
    
    for cmd in build_cmds:
        print(f"\n빌드 중: {cmd[-1]}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            print(f"✅ 빌드 성공: {cmd[-1]}")
        else:
            print(f"❌ 빌드 실패: {cmd[-1]}")
            print(result.stderr)
    
    # 결과 확인
    dist_dir = Path("dist")
    if dist_dir.exists():
        print(f"\n📦 생성된 파일:")
        for f in dist_dir.glob("*.exe"):
            size = f.stat().st_size / (1024*1024)
            print(f"  {f.name} ({size:.1f} MB)")


if __name__ == "__main__":
    build_exe()
