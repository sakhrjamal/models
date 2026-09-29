@echo off
chcp 65001 >nul
title نظام إدارة الإنتاج والتتبع
cd /d "%~dp0"
call "%~dp0إعدادات_التشغيل.bat"
rem التشغيل اليدوي يختار منفذا حرا تلقائيا؛ المنفذ الثابت للخدمة فقط
set "SAVED_PORT=%QMS_PORT%"
set "QMS_PORT="

echo ============================================================
echo   نظام إدارة الإنتاج والتتبع
echo   مكان البيانات: %QMS_DATA%
echo ============================================================
echo.

rem اذا كان النظام يعمل كخدمة ويندوز: افتح المتصفح فقط
powershell -NoProfile -Command "try{ if((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:%SAVED_PORT%/health).StatusCode -eq 200){exit 0} }catch{}; exit 1" >nul 2>&1
if not errorlevel 1 (
  echo النظام يعمل بالفعل كخدمة ويندوز - فتح المتصفح...
  start "" "http://127.0.0.1:%SAVED_PORT%"
  timeout /t 3 >nul
  exit /b 0
)

if not exist "%QMS_DATA:~0,3%" (
  echo [خطأ] القرص %QMS_DATA:~0,2% غير موجود على هذا الجهاز.
  echo       لن يبدأ النظام حتى لا تُنشأ قاعدة بيانات في مكان اخر بالخطأ.
  echo       اضبط  QMS_DATA  في ملف إعدادات_التشغيل.bat  او وصّل القرص.
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
