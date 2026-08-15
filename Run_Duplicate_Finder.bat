@echo off
:: Enable UTF-8 encoding in console if possible, but keep standard output for compatibility
chcp 65001 >nul 2>&1
title Duplicate File Finder CLI

:: Set color scheme: light aqua text on black background
color 0B

echo =======================================================================
echo          ██████╗ ██╗   ██╗██████╗      ███████╗██╗███╗   ██╗██████╗ 
echo          ██╔══██╗██║   ██║██╔══██╗     ██╔════╝██║████╗  ██║██╔══██╗
echo          ██║  ██║██║   ██║██████╔╝     █████╗  ██║██╔██╗ ██║██║  ██║
echo          ██║  ██║██║   ██║██╔═══╝      ██╔══╝  ██║██║╚██╗██║██║  ██║
echo          ██████╔╝╚██████╔╝██║          ██║     ██║██║ ╚████║██████╔╝
echo          ╚══════╝  ╚═════╝ ╚═╝          ╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝ 
echo =======================================================================
echo.

set "SCAN_DIR=%~1"
set "ISOLATE_DIR="

:: Check if user dragged and dropped a folder
if not "%SCAN_DIR%"=="" (
    echo [INFO] Detected drag-and-drop folder:
    echo        "%SCAN_DIR%"
    echo.
    goto GET_ISOLATE
)

:GET_SCAN
echo Please enter the folder path you want to scan for duplicates.
echo (Or drag and drop the folder directly into this window and press Enter)
echo.
set /p "SCAN_DIR=Scan Path: "
:: Remove enclosing quotes if pasted with quotes
set "SCAN_DIR=%SCAN_DIR:"=%"

if "%SCAN_DIR%"=="" (
    echo Error: Scan path cannot be empty.
    echo.
    goto GET_SCAN
)

if not exist "%SCAN_DIR%" (
    echo Error: The folder "%SCAN_DIR%" does not exist.
    echo.
    goto GET_SCAN
)

:GET_ISOLATE
echo.
echo Please enter the target folder path where duplicate copies will be moved.
echo (e.g. C:\Isolated_Duplicates or D:\Duplicates_Review)
echo.
set /p "ISOLATE_DIR=Isolation Path: "
set "ISOLATE_DIR=%ISOLATE_DIR:"=%"

if "%ISOLATE_DIR%"=="" (
    echo Error: Isolation path cannot be empty.
    echo.
    goto GET_ISOLATE
)

echo.
echo =======================================================================
echo  Starting duplicate finder pipeline...
echo =======================================================================
echo.

:: Run the python script in the directory of this batch file
python "%~dp0duplicate_finder.py" run "%SCAN_DIR%" "%ISOLATE_DIR%"

echo.
echo =======================================================================
echo  Process Completed. Press any key to exit this window...
echo =======================================================================
pause >nul
