@echo off
echo ============================================
echo  LinMacro v4 Install
echo ============================================
echo.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Run as Administrator
    pause
    exit /b 1
)
echo [1/3] testsigning on...
bcdedit /set testsigning on
echo [2/3] LcHide driver install...
sc query LcHide >nul 2>&1
if %errorlevel% equ 0 (
    sc stop LcHide >nul 2>&1
    sc delete LcHide >nul 2>&1
    timeout /t 2 >nul
)
sc create LcHide type= kernel binPath= "%~dp0driver\lchide.sys"
sc start LcHide
if %errorlevel% equ 0 (
    echo    [OK] Driver running
) else (
    echo    [FAIL] Reboot and retry
)
echo [3/3] Shortcut...
powershell -Command "=New-Object -ComObject WScript.Shell; =.CreateShortcut('%USERPROFILE%\Desktop\LinMacro_v4.lnk'); .TargetPath='%~dp0linmacro_v4.exe'; .WorkingDirectory='%~dp0'; .Save()"
echo.
echo  Done! Run LinMacro_v4 on desktop
pause
