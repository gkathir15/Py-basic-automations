@echo off
title Duplicate File Finder GUI Launcher
where pythonw >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    start "" pythonw "%~dp0duplicate_finder.py"
) else (
    start "" python "%~dp0duplicate_finder.py"
)
