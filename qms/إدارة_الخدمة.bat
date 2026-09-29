@echo off
chcp 65001 >nul
title إدارة خدمة نظام الانتاج والتتبع
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service_tools.ps1"
echo.
pause
