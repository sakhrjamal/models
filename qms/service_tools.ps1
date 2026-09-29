# إدارة تشغيل النظام كخدمة ويندوز (مهمة مجدولة عند الإقلاع باسم SYSTEM، تُعاد تلقائيًا إن توقفت)
param([string]$Action = 'menu')
$ErrorActionPreference = 'Stop'
$dir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$task = 'QMS_System'
$fw   = 'QMS System (TCP)'

function Test-Admin { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator') }
if (-not (Test-Admin)) {
    Write-Host 'يلزم تشغيل هذا الملف كمسؤول — سيُعاد فتحه بصلاحية المسؤول...'
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action $Action"
    exit
}

function Get-Setting([string]$name) {
    $text = Get-Content (Join-Path $dir 'إعدادات_التشغيل.bat') -Raw -Encoding UTF8
    $m = [regex]::Match($text, 'set "' + $name + '=([^"]*)"')
    if ($m.Success) { $m.Groups[1].Value } else { $null }
}
$data = Get-Setting 'QMS_DATA'; $port = Get-Setting 'QMS_PORT'
if (-not $port) { $port = '5000' }

function Get-Health {
    try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$port/health").StatusCode -eq 200 } catch { $false }
}
function Get-LanIp {
    (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } | Select-Object -First 1).IPAddress
}
function Stop-Qms {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'run\.py' -and $_.CommandLine -like "*python*" -and $_.CommandLine -notmatch 'service_tools' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Install-Qms {
    if (-not $data) { throw 'لم يوجد QMS_DATA في إعدادات_التشغيل.bat' }
    $drive = Split-Path -Qualifier $data
    if ($drive -and -not (Test-Path "$drive\")) { throw "القرص $drive غير موجود — لن أُثبّت الخدمة" }
    $py = (& python -c "import sys;print(sys.executable)").Trim()
    if (-not (Test-Path $py)) { throw 'بايثون غير موجود في PATH' }
    Write-Host "بايثون: $py"
    & $py -m pip install --quiet flask openpyxl waitress
    New-Item -ItemType Directory -Force -Path $data | Out-Null
    $env:QMS_DATA = $data
    if (-not (Test-Path (Join-Path $data 'qms.db'))) {
        $old = Join-Path $dir 'data\qms.db'
        if (Test-Path $old) { Copy-Item $old (Join-Path $data 'qms.db'); $k = Join-Path $dir 'data\secret.key'; if (Test-Path $k) { Copy-Item $k $data } }
        else { Push-Location $dir; & $py seed.py | Out-Null; Pop-Location }
    }
    # ملف التشغيل الذي تنفذه الخدمة (مسارات مطلقة)
    $run = Join-Path $dir '_service_run.bat'
    $txt = "@echo off`r`nchcp 65001 >nul`r`nset `"QMS_DATA=$data`"`r`nset `"QMS_PORT=$port`"`r`nset `"QMS_SERVICE=1`"`r`nset `"PYTHONUTF8=1`"`r`ncd /d `"$dir`"`r`n`"$py`" run.py`r`n"
    [IO.File]::WriteAllText($run, $txt, (New-Object Text.UTF8Encoding($false)))
    Stop-Qms
    $act = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$run`"" -WorkingDirectory $dir
    $trg = New-ScheduledTaskTrigger -AtStartup; $trg.Delay = 'PT30S'
    $prn = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
           -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $task -Action $act -Trigger $trg -Principal $prn -Settings $set -Force -Description 'نظام إدارة الإنتاج والتتبع' | Out-Null
    Get-NetFirewallRule -DisplayName $fw -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $fw -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Domain,Private | Out-Null
    Start-ScheduledTask -TaskName $task
    Write-Host 'تم التثبيت. انتظر بدء النظام...'
    1..20 | ForEach-Object { if (-not (Get-Health)) { Start-Sleep -Seconds 1 } }
    Show-Status
}

function Show-Status {
    $t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host 'الخدمة غير مثبّتة.'; return }
    Write-Host ("حالة المهمة   : " + $t.State)
    Write-Host ("يستجيب النظام : " + $(if (Get-Health) { 'نعم' } else { 'لا' }))
    Write-Host "على هذا الجهاز: http://127.0.0.1:$port"
    Write-Host ("من الأجهزة الأخرى: http://" + (Get-LanIp) + ":$port")
    Write-Host "مجلد البيانات : $data"
}

function Remove-Qms {
    Stop-Qms
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $task -Confirm:$false }
    Get-NetFirewallRule -DisplayName $fw -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host 'أُزيلت الخدمة وقاعدة جدار الحماية. البيانات لم تُحذف.'
}

function Invoke-Action([string]$a) {
    switch ($a) {
        'install' { Install-Qms }
        'start'   { if (-not (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue)) { Write-Host 'ثبّت الخدمة أولًا'; return }; Start-ScheduledTask -TaskName $task; Start-Sleep -Seconds 5; Show-Status }
        'stop'    { Stop-Qms; Write-Host 'أُوقف النظام.' }
        'status'  { Show-Status }
        'remove'  { Remove-Qms }
    }
}

try {
    if ($Action -ne 'menu') { Invoke-Action $Action }
    else {
        while ($true) {
            Write-Host "`n=== إدارة خدمة نظام الانتاج والتتبع ===" 
            Write-Host '1) تثبيت الخدمة وتشغيلها (تعمل مع اقلاع الجهاز بلا تسجيل دخول)'
            Write-Host '2) تشغيل   3) إيقاف   4) الحالة   5) إزالة الخدمة   0) خروج'
            $c = Read-Host 'اختر'
            switch ($c) { '1' { Invoke-Action 'install' } '2' { Invoke-Action 'start' } '3' { Invoke-Action 'stop' } '4' { Invoke-Action 'status' } '5' { Invoke-Action 'remove' } '0' { break } }
            if ($c -eq '0') { break }
        }
    }
} catch { Write-Host "[خطأ] $_" -ForegroundColor Red; Read-Host 'اضغط Enter' }
