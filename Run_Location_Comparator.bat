@echo off
:: Enable UTF-8 encoding in console if possible
chcp 65001 >nul 2>&1
title Location Hash Comparator CLI

:: Set color scheme: light aqua text on black background
color 0B

echo =======================================================================
echo     ██╗      ██████╗  ██████╗ █████╗ ████████╗██╗  ██╗ ██████╗ ███╗   ██╗
echo     ██║     ██╔═══██╗██╔════╝██╔══██╗╚══██╔══╝██║  ██║██╔═══██╗████╗  ██║
echo     ██║     ██║   ██║██║     ███████║   ██║   ███████║██║   ██║██╔██╗ ██║
echo     ██║     ██║   ██║██║     ██╔══██║   ██║   ██╔══██║██║   ██║██║╚██╗██║
echo     ███████╗╚██████╔╝╚██████╗██║  ██║   ██║   ██║  ██║╚██████╔╝██║ ╚████║
echo     ╚══════╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝
echo =======================================================================
echo.

set "LOC_A=%~1"
set "LOC_B=%~2"
set "ISOLATE_DIR=%~3"

if not "%LOC_A%"=="" if not "%LOC_B%"=="" (
    echo [INFO] Using command line parameters for Location A and B:
    echo        Loc A: "%LOC_A%"
    echo        Loc B: "%LOC_B%"
    echo.
    if not "%ISOLATE_DIR%"=="" goto START_RUN
    goto GET_ISOLATE
)

:GET_LOC_A
echo Please enter the folder path for Location A.
echo.
set /p "LOC_A=Location A Path: "
set "LOC_A=%LOC_A:"=%"

if "%LOC_A%"=="" (
    echo Error: Location A path cannot be empty.
    echo.
    goto GET_LOC_A
)

if not exist "%LOC_A%" (
    echo Error: The folder "%LOC_A%" does not exist.
    echo.
    goto GET_LOC_A
)

:GET_LOC_B
echo.
echo Please enter the folder path for Location B.
echo.
set /p "LOC_B=Location B Path: "
set "LOC_B=%LOC_B:"=%"

if "%LOC_B%"=="" (
    echo Error: Location B path cannot be empty.
    echo.
    goto GET_LOC_B
)

if not exist "%LOC_B%" (
    echo Error: The folder "%LOC_B%" does not exist.
    echo.
    goto GET_LOC_B
)

:GET_ISOLATE
echo.
echo Please enter the target folder path where differing files will be isolated.
echo (e.g. C:\Isolated_Differences)
echo.
set /p "ISOLATE_DIR=Isolation Path: "
set "ISOLATE_DIR=%ISOLATE_DIR:"=%"

if "%ISOLATE_DIR%"=="" (
    echo Error: Isolation path cannot be empty.
    echo.
    goto GET_ISOLATE
)

:START_RUN
echo.
echo =======================================================================
echo  Starting location comparison pipeline...
echo =======================================================================
echo.

python "%~dp0location_comparator.py" run "%LOC_A%" "%LOC_B%" "%ISOLATE_DIR%"

echo.
echo =======================================================================
echo  Process Completed. Press any key to exit this window...
echo =======================================================================
pause >nul
