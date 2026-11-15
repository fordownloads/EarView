@echo off
rem Live view of the iTiMO / MZ otoscope in ffplay.
rem Press Ctrl+C in this window (or close the ffplay window) to stop.
python "%~dp0earpick_video.py" play %*
