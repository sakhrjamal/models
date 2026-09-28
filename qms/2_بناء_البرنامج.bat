@echo off
chcp 65001 >nul
title بناء النظام
cd /d "%~dp0"

echo ============================================================
echo   بناء الملف التنفيذي
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 ( echo [خطأ] بايثون غير موجود في PATH & pause & exit /b 1 )

echo [1/4] تركيب المكتبات...
python -m pip install --quiet flask openpyxl waitress pyinstaller
if errorlevel 1 ( echo [خطأ] فشل تركيب المكتبات & pause & exit /b 1 )

echo [2/4] تجهيز قاعدة البيانات...
if not exist "data\qms.db" ( python seed.py )

echo [3/4] بناء الملف التنفيذي - قد يستغرق عدة دقائق...
python -m PyInstaller --noconfirm --onefile --name QMS_System --add-data "templates;templates" --add-data "static;static" --add-data "schema.sql;." --hidden-import waitress --hidden-import jinja2 --console run.py > build_log.txt 2>&1

if not exist "dist\QMS_System.exe" (
  echo.
  echo [خطأ] لم يكتمل البناء
  echo تفاصيل الخطأ في الملف: build_log.txt
  echo افتحه وارسل لي آخر عشرين سطرا منه
  echo.
  pause & exit /b 1
)

echo [4/4] تجهيز مجلد التشغيل...
if not exist "dist\data" mkdir "dist\data"
if not exist "dist\data\qms.db" copy "data\qms.db" "dist\data\qms.db" >nul

echo.
echo ============================================================
echo   تم البناء بنجاح
echo.
echo   البرنامج : dist\QMS_System.exe
echo   القاعدة  : dist\data\qms.db   ^<-- انسخه يوميا
echo.
echo   انقل مجلد dist كاملا الى سطح المكتب
echo ============================================================
pause
