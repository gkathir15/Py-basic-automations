@echo off
:: Enable UTF-8 encoding in console if possible
chcp 65001 >nul 2>&1
title Drive Sync CLI

:: Set color scheme: light green text on black background
color 0A

echo =======================================================================
echo     ██████╗ ██████╗ ██╗██╗   ██╗███████╗    ███████╗██╗   ██╗███╗   ██╗ ██████╗
echo     ██╔══██╗██╔══██╗██║██║   ██║██╔════╝    ██╔════╝╚██╗ ██╔╝████╗  ██║██╔════╝
echo     ██║  ██║██████╔╝██║██║   ██║█████╗      ███████╗ ╚████╔╝ ██╔██╗ ██║██║
echo     ██║  ██║██╔══██╗██║╚██╗ ██╔╝██╔══╝      ╚════██║  ╚██╔╝  ██║╚██╗██║██║
echo     ██████╔╝██║  ██║██║ ╚████╔╝ ███████╗    ███████║   ██║   ██║ ╚████║╚██████╗
echo     ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝  ╚══════╝    ╚══════╝   ╚═╝   ╚═╝  ╚═══╝ ╚═════╝
echo =======================================================================
echo  One-way sync of missing files between two drives (hash verified).
echo  The destination keeps its extra files; nothing is ever deleted there.
echo =======================================================================
echo.

set "SRC=%~1"
set "DST=%~2"
set "MATCH=%~3"

if not "%SRC%"=="" if not "%DST%"=="" goto START_RUN

:GET_SRC
echo Please enter the SOURCE drive or folder (files are copied FROM here).
echo (e.g. D:\  or  D:\Photos)
echo.
set /p "SRC=Source Path: "
set "SRC=%SRC:"=%"

if "%SRC%"=="" (
    echo Error: Source path cannot be empty.
    echo.
    goto GET_SRC
)

if not exist "%SRC%" (
    echo Error: The source path "%SRC%" does not exist.
    echo.
    goto GET_SRC
)

:GET_DST
echo.
echo Please enter the DESTINATION drive or folder (files are copied TO here).
echo (e.g. E:\Backup)
echo.
set /p "DST=Destination Path: "
set "DST=%DST:"=%"

if "%DST%"=="" (
    echo Error: Destination path cannot be empty.
    echo.
    goto GET_DST
)

if not exist "%DST%" (
    echo Error: The destination path "%DST%" does not exist.
    echo.
    goto GET_DST
)

:GET_MATCH
echo.
echo How should the tool decide a source file is already present?
echo   [1] Content match  - identical content anywhere in the destination (default)
echo   [2] Path match     - the same relative path exists (differences are only reported)
echo   [3] Path + content - same relative path with identical content
echo.
set "MATCH_CHOICE="
set /p "MATCH_CHOICE=Choose 1, 2 or 3 (ENTER for 1): "

if "%MATCH_CHOICE%"=="2" set "MATCH=path"
if "%MATCH_CHOICE%"=="3" set "MATCH=both"
if "%MATCH%"=="" set "MATCH=hash"

:START_RUN
if "%MATCH%"=="" set "MATCH=hash"

echo.
echo =======================================================================
echo  Starting drive sync pipeline (match mode: %MATCH%)...
echo  A dry-run plan is shown first, then you are asked to confirm.
echo =======================================================================
echo.

python "%~dp0drive_sync.py" run "%SRC%" "%DST%" --match %MATCH%

echo.
echo =======================================================================
echo  Process Completed. Press any key to exit this window...
echo =======================================================================
pause >nul
