@echo off
chcp 65001 >nul
title نظام إدارة الإنتاج والتتبع
cd /d "%~dp0"

rem ============================================================
rem   مكان تخزين البيانات (قاعدة البيانات + النسخ الاحتياطية + السجلات)
rem   غيّر السطر التالي فقط إن أردت مكانا اخر
rem ============================================================
set "QMS_DATA=D:\QMS_Data"

echo ============================================================
echo   نظام إدارة الإنتاج والتتبع
echo   مكان البيانات: %QMS_DATA%
echo ============================================================
echo.

if not exist "%QMS_DATA:~0,3%" (
  echo [خطأ] القرص %QMS_DATA:~0,2% غير موجود على هذا الجهاز.
  echo       لن يبدأ النظام حتى لا تُنشأ قاعدة بيانات في مكان اخر بالخطأ.
  echo       اضبط السطر  set "QMS_DATA=..."  في هذا الملف، او وصّل القرص.
  echo.
  pause & exit /b 1
)

python --version >nul 2>&1
if errorlevel 1 (
  echo [خطأ] بايثون غير موجود في PATH - اعد تركيبه من python.org وفعّل "Add Python to PATH"
  pause & exit /b 1
)

python -c "import flask, openpyxl, waitress" >nul 2>&1
if errorlevel 1 (
  echo تركيب المكتبات لاول مرة، انتظر قليلا...
  python -m pip install --quiet flask openpyxl waitress
  if errorlevel 1 ( echo [خطأ] فشل تركيب المكتبات - تحقق من الانترنت & pause & exit /b 1 )
)

if not exist "%QMS_DATA%" mkdir "%QMS_DATA%"
if not exist "%QMS_DATA%\qms.db" (
  if exist "data\qms.db" (
    echo وُجدت بيانات قديمة داخل مجلد البرنامج - نسخها الى %QMS_DATA% ...
    copy /y "data\qms.db" "%QMS_DATA%\qms.db" >nul
    if exist "data\secret.key" copy /y "data\secret.key" "%QMS_DATA%\secret.key" >nul
    if exist "data\letterhead.*" copy /y "data\letterhead.*" "%QMS_DATA%\" >nul
    echo تم. النسخة القديمة بقيت كما هي في مجلد البرنامج ^(لا تستعملها بعد الان^).
  ) else (
    echo انشاء قاعدة البيانات لاول مرة في %QMS_DATA% ...
    python seed.py
    if errorlevel 1 ( echo [خطأ] فشل انشاء القاعدة & pause & exit /b 1 )
  )
)

echo.
echo تشغيل النظام... ^(لا تغلق هذه النافذة اثناء العمل؛ صغّرها فقط^)
echo.
python run.py

echo.
echo توقف النظام.
pause
