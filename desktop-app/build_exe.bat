@echo off
python "%~dp0build_exe.py" --onefile
if errorlevel 1 pause
