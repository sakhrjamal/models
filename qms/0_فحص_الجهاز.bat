@echo off
chcp 65001 >nul
title فحص الجهاز
cd /d "%~dp0"

echo ============================================================
echo   فحص جاهزية الجهاز
echo ============================================================
echo.

echo [1] بايثون:
python --version 2>&1
if errorlevel 1 (
  echo     غير موجود في PATH  ^<-- هذه هي المشكلة
  echo     الحل: أعد تركيب بايثون وفعّل "Add Python to PATH"
) else (
  echo     سليم
)
echo.

echo [2] مكان بايثون:
where python 2>&1
echo.

echo [3] pip:
python -m pip --version 2>&1
echo.

echo [4] المكتبات:
python -c "import flask; print('    flask   :', flask.__version__)" 2>&1
python -c "import openpyxl; print('    openpyxl:', openpyxl.__version__)" 2>&1
python -c "import sqlite3; print('    sqlite3 : سليم')" 2>&1
echo.

echo [5] ملفات النظام:
if exist "run.py"       (echo     run.py       موجود) else (echo     run.py       مفقود)
if exist "app.py"       (echo     app.py       موجود) else (echo     app.py       مفقود)
if exist "seed.py"      (echo     seed.py      موجود) else (echo     seed.py      مفقود)
if exist "schema.sql"   (echo     schema.sql   موجود) else (echo     schema.sql   مفقود)
if exist "templates"    (echo     templates    موجود) else (echo     templates    مفقود)
if exist "static"       (echo     static       موجود) else (echo     static       مفقود)
if exist "data\qms.db"  (echo     qms.db       موجود) else (echo     qms.db       غير منشأ بعد)
echo.

echo [6] المجلد الحالي:
cd
echo.
echo ============================================================
echo   انسخ كل ما ظهر أعلاه وأرسله لي إن استمرت المشكلة
echo ============================================================
pause
