@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\نظام الانتاج والتتبع.lnk');" ^
 "$s.TargetPath='%~dp0تشغيل_النظام.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.IconLocation='shell32.dll,13'; $s.Description='نظام إدارة الإنتاج والتتبع'; $s.Save()"
if errorlevel 1 ( echo [خطأ] تعذر انشاء الاختصار & pause & exit /b 1 )
echo تم انشاء اختصار "نظام الانتاج والتتبع" على سطح المكتب.
echo يفتح مصغّرا؛ اضغط عليه مرتين لتشغيل النظام.
pause
