@echo off
where pythonw >nul 2>&1
if %errorlevel% equ 0 (
    start "" pythonw "%~dp0duplicate_finder.py"
) else (
    start "" python "%~dp0duplicate_finder.py"
)
