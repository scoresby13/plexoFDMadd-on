@echo off
rem Starts the Plexo bridge in the background and opens its status page.
rem Needs Python 3.10 or later: https://www.python.org/downloads/
setlocal
set "SCRIPT=%~dp0bridge\plexo_fdm.py"
where pyw >nul 2>nul && (pyw -3 "%SCRIPT%" ui & exit /b 0)
where pythonw >nul 2>nul && (pythonw "%SCRIPT%" ui & exit /b 0)
where py >nul 2>nul && (py -3 "%SCRIPT%" ui & exit /b 0)
where python >nul 2>nul && (python "%SCRIPT%" ui & exit /b 0)
echo Python wasn't found.
echo Install Python 3 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo then run this file again.
pause
exit /b 1
