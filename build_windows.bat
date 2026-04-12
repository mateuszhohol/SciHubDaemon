@echo off
echo ============================================
echo  Building Researcher's Best Friend for Windows
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
pyinstaller --name "Researchers Best Friend" --onefile --windowed --noconfirm search_full_paper.py
if %errorlevel% neq 0 (
    echo ERROR: Build failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  BUILD COMPLETE!
echo  Your .exe is in: dist\Researchers Best Friend.exe
echo ============================================
pause
