@echo off
rem KB Helper launcher for Windows: kbhelper [start^|stop^|restart^|status^|logs^|install]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\kbhelper.ps1" %*
