@echo off
chcp 65001 >nul
cd /d "%~dp0"

python -c "import PySide6, fitz, numpy" 2>nul
if errorlevel 1 (
    echo [!] 缺少依赖，请先执行：pip install -r requirements.txt
    pause
    exit /b 1
)

where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python "reader.py"
) else (
    start "" pythonw "reader.py"
)
