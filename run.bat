@echo off
rem TouchBridge -> RPE Windows one-click launcher (thin wrapper).
rem The real logic lives in run.ps1 so we can print proper UTF-8 text.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"