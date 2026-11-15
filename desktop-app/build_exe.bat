@echo off
python "%~dp0build_exe.py"
if errorlevel 1 pause
