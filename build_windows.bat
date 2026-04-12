@echo off
echo ============================================
echo  Building SciHubDaemon for Windows
echo ============================================
echo.

echo Installing dependencies...
pip install requests beautifulsoup4 pyinstaller
if %errorlevel% neq 0 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo Building .exe...
pyinstaller --name "SciHubDaemon" --windowed --icon=icon.ico --noconfirm scihubdaemon.py
if %errorlevel% neq 0 (
    echo ERROR: Build failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  BUILD COMPLETE!
echo  Your app is in: dist\SciHubDaemon\
echo ============================================
pause
