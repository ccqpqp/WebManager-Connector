@echo off
cd /d "%~dp0"
start "" wscript.exe "%~dp0WebManager-Start.vbs"
exit /b 0
