@echo off
chcp 65001 >nul
title نظام إدارة الإنتاج والتتبع
cd /d "%~dp0"

echo ============================================================
echo   نظام إدارة الإنتاج والتتبع - تشغيل مباشر
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo [خطأ] بايثون غير موجود في PATH
  echo.
  echo الحل: أعد تركيب بايثون من python.org
  echo وفعّل الخيار "Add Python to PATH" في أول شاشة
  echo.
  pause & exit /b 1
)

echo [1/3] التحقق من المكتبات...
python -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo       تركيب المكتبات لأول مرة، انتظر قليلا...
  python -m pip install --quiet flask openpyxl waitress
  if errorlevel 1 (
    echo [خطأ] فشل تركيب المكتبات - تحقق من الاتصال بالانترنت
    pause & exit /b 1
  )
)

echo [2/3] التحقق من قاعدة البيانات...
if not exist "data\qms.db" (
  echo       انشاء قاعدة البيانات...
  python seed.py
  if errorlevel 1 ( echo [خطأ] فشل انشاء القاعدة & pause & exit /b 1 )
)

echo [3/3] تشغيل النظام...
echo.
python run.py

echo.
echo توقف النظام.
pause
