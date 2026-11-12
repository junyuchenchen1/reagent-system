# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    检验科试剂管理系统 - 一键部署脚本
.DESCRIPTION
    在一台新电脑上运行本脚本即可完成：
      1. 检查 Python 与依赖
      2. 初始化数据库 / 备份目录
      3. 生成启动 / 停止 / 看门狗 VBS（GBK 编码）
      4. 注册看门狗计划任务（每 5 分钟检测崩溃自动拉起）
      5. 注册每日备份计划任务（每天 23:00）
      6. 放入登录自启快捷方式
      7. 立即启动服务
    卸载请运行 uninstall.ps1（如需要）。
.EXAMPLE
    右键本文件 → 使用 PowerShell 运行
    或： powershell -ExecutionPolicy Bypass -File install.ps1
#>

$ErrorActionPreference = "Stop"
$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $proj

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  检验科试剂管理系统 - 一键部署" -ForegroundColor Cyan
Write-Host "  项目目录: $proj" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ---------- 1. 检查 Python ----------
Write-Host "`n[1/8] 检查 Python ..." -ForegroundColor Yellow
try {
    $pyVer = python --version 2>&1
    Write-Host "  已安装: $pyVer" -ForegroundColor Green
} catch {
    Write-Host "  [错误] 未检测到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
# 版本校验：项目依赖 Flask 3 / SQLAlchemy 2，要求 Python 3.10 及以上
$pyNum = (python -c "import sys; print(sys.version_info[0], sys.version_info[1])" 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $pyNum) {
    Write-Host "  [错误] 无法获取 Python 版本，请确认 python 命令可用" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
$pyParts = $pyNum.Trim() -split "\s+"
$pyMajor = [int]$pyParts[0]
$pyMinor = [int]$pyParts[1]
if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 10)) {
    Write-Host "  [错误] 当前 Python 为 $pyMajor.$pyMinor，低于 3.10，请升级后重试（https://www.python.org/downloads/）" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
Write-Host "  版本检查通过：Python $pyMajor.$pyMinor（要求 3.10 及以上）" -ForegroundColor Green

# ---------- 2. 安装依赖 ----------
Write-Host "`n[2/8] 安装 Python 依赖 ..." -ForegroundColor Yellow
python -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [警告] pip 安装依赖失败，请手动运行: pip install -r requirements.txt" -ForegroundColor Red
} else {
    Write-Host "  依赖安装完成" -ForegroundColor Green
}

# ---------- 3. 创建目录 ----------
Write-Host "`n[3/8] 创建工作目录 ..." -ForegroundColor Yellow
foreach ($d in @("instance", "logs", "backups", "exports", "instance\signatures", "instance\certs")) {
    $p = Join-Path $proj $d
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
}
Write-Host "  instance / logs / backups / exports / signatures / certs 就绪" -ForegroundColor Green

# ---------- 3.5 清理 Python 字节码缓存（避免旧 .pyc 导致新代码不生效）----------
Write-Host "`n[3.5] 清理 __pycache__ 缓存 ..." -ForegroundColor Yellow
Get-ChildItem -Path $proj -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Write-Host "  已清理所有 __pycache__" -ForegroundColor Green

# ---------- 4. 写 VBS 脚本（GBK 编码，兼容中文 Windows）----------
Write-Host "`n[4/8] 生成 VBS 启动/停止/看门狗脚本 ..." -ForegroundColor Yellow
$gbk = [System.Text.Encoding]::GetEncoding("GB2312")

$startVbs = @'
Option Explicit
Dim fso, shell, baseDir, logDir, cmd, code, silent, ip
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
silent = False
If WScript.Arguments.Count > 0 Then
    If WScript.Arguments(0) = "silent" Then silent = True
End If
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = baseDir & "\logs"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
cmd = "powershell -NoProfile -Command ""$c = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 1 } else { exit 0 }"""
code = shell.Run(cmd, 0, True)
If code = 1 Then
    If Not silent Then MsgBox "服务已在端口 5000 运行中。", vbInformation
    WScript.Quit
End If
shell.CurrentDirectory = baseDir
cmd = "cmd /c python run.py >> """ & logDir & "\server.log"" 2>&1"
shell.Run cmd, 0, False
WScript.Sleep 2000
On Error Resume Next
Dim exec, output, lines, i, parts
Set exec = shell.Exec("ipconfig")
output = exec.StdOut.ReadAll
ip = ""
lines = Split(output, vbCrLf)
For i = 0 To UBound(lines)
    If InStr(lines(i), "IPv4") > 0 Then
        parts = Split(lines(i), ":")
        If UBound(parts) >= 1 Then
            ip = Trim(parts(1))
            If Left(ip, 3) <> "127" Then Exit For
        End If
    End If
Next
If ip = "" Then ip = "127.0.0.1"
If Not silent Then MsgBox "检验科试剂管理系统已启动。" & vbCrLf & vbCrLf & "本机访问:  http://127.0.0.1:5000" & vbCrLf & "局域网访问: http://" & ip & ":5000", vbInformation
'@

$stopVbs = @'
Option Explicit
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell -NoProfile -Command ""Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }""", 0, True
MsgBox "服务已停止。", vbInformation
'@

$watchdogVbs = @'
Option Explicit
Dim fso, shell, baseDir, cmd, code
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell -NoProfile -Command ""$c = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 1 } else { exit 0 }"""
code = shell.Run(cmd, 0, True)
If code = 0 Then
    shell.CurrentDirectory = baseDir
    shell.Run "cmd /c python run.py >> """ & baseDir & "\logs\server.log"" 2>&1", 0, False
End If
'@

[System.IO.File]::WriteAllText((Join-Path $proj "start_server.vbs"), $startVbs, $gbk)
[System.IO.File]::WriteAllText((Join-Path $proj "stop_server.vbs"), $stopVbs, $gbk)
[System.IO.File]::WriteAllText((Join-Path $proj "watchdog.vbs"), $watchdogVbs, $gbk)
Write-Host "  start_server.vbs / stop_server.vbs / watchdog.vbs 已生成 (GBK)" -ForegroundColor Green

# ---------- 5. 注册计划任务 ----------
Write-Host "`n[5/8] 注册计划任务（看门狗 + 每日备份）..." -ForegroundColor Yellow
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

# 看门狗：每 5 分钟
$wdAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$proj\watchdog.vbs`""
$wdStart = (Get-Date).Date.AddMinutes(2)
$wdTrigger = New-ScheduledTaskTrigger -Once -At $wdStart
$wdTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At $wdStart -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition
try {
    Register-ScheduledTask -TaskName "ReagentServer_Watchdog" -Action $wdAction -Trigger $wdTrigger -Settings $settings -Force | Out-Null
    Write-Host "  看门狗任务 (每5分钟): OK" -ForegroundColor Green
} catch {
    Write-Host "  [警告] 看门狗任务注册失败: $_" -ForegroundColor Red
}

# 每日备份 23:00
$bkAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c python `"$proj\backup.py`" >> `"$proj\logs\backup.log`" 2>&1"
$bkTrigger = New-ScheduledTaskTrigger -Daily -At "23:00"
try {
    Register-ScheduledTask -TaskName "ReagentServer_Backup" -Action $bkAction -Trigger $bkTrigger -Settings $settings -Force | Out-Null
    Write-Host "  每日备份任务 (23:00): OK" -ForegroundColor Green
} catch {
    Write-Host "  [警告] 备份任务注册失败: $_" -ForegroundColor Red
}

# ---------- 6. 登录自启 + 桌面快捷方式 ----------
Write-Host "`n[6/8] 写入登录自启与桌面快捷方式 ..." -ForegroundColor Yellow
$startupDir = [Environment]::GetFolderPath("Startup")
$ws = New-Object -ComObject WScript.Shell

# 登录自启
$lnkPath = Join-Path $startupDir "试剂管理系统.lnk"
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath = "wscript.exe"
$sc.Arguments = "`"$proj\start_server.vbs`" silent"
$sc.WorkingDirectory = $proj
$sc.WindowStyle = 7
$sc.Description = "Reagent Management System AutoStart"
$sc.Save()
Write-Host "  启动文件夹快捷方式: $lnkPath" -ForegroundColor Green

# 桌面快捷方式：启动
$desktopDir = [Environment]::GetFolderPath("Desktop")
$lnkStart = Join-Path $desktopDir "试剂管理系统.lnk"
$sc1 = $ws.CreateShortcut($lnkStart)
$sc1.TargetPath = "wscript.exe"
$sc1.Arguments = "`"$proj\start_server.vbs`""
$sc1.WorkingDirectory = $proj
$sc1.WindowStyle = 7
$sc1.Description = "启动检验科试剂管理系统"
$sc1.Save()
Write-Host "  桌面快捷方式(启动): $lnkStart" -ForegroundColor Green

# 桌面快捷方式：停止
$lnkStop = Join-Path $desktopDir "停止试剂管理系统.lnk"
$sc2 = $ws.CreateShortcut($lnkStop)
$sc2.TargetPath = "wscript.exe"
$sc2.Arguments = "`"$proj\stop_server.vbs`""
$sc2.WorkingDirectory = $proj
$sc2.WindowStyle = 7
$sc2.Description = "停止检验科试剂管理系统"
$sc2.Save()
Write-Host "  桌面快捷方式(停止): $lnkStop" -ForegroundColor Green

# ---------- 7. 立即启动服务 ----------
Write-Host "`n[7/8] 启动服务 ..." -ForegroundColor Yellow
wscript.exe (Join-Path $proj "start_server.vbs") silent
Start-Sleep -Seconds 6
$listening = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "  服务已启动: http://127.0.0.1:5000" -ForegroundColor Green
} else {
    Write-Host "  [提示] 服务可能还在启动中，请稍后访问 http://127.0.0.1:5000" -ForegroundColor Yellow
}

# ---------- 完成 ----------
Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  部署完成！" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host @"
  日常使用：
    - 登录 Windows 后服务自动启动（无需手动操作）
    - 服务崩溃会在 5 分钟内自动拉起
    - 每天 23:00 自动备份数据库到 backups\ 目录
    - 手动启动：双击 start_server.vbs
    - 手动停止：双击 stop_server.vbs
    - 手动备份：双击运行 backup.py
"@ -ForegroundColor White

$lanIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" } | Select-Object -First 1).IPAddress
if ($lanIP) {
    Write-Host "`n  局域网其他电脑访问：" -ForegroundColor Green
    Write-Host "    http://$lanIP`:5000`n" -ForegroundColor Yellow
} else {
    Write-Host "`n  局域网其他电脑访问：http://本机IP:5000`n" -ForegroundColor Yellow
}
Read-Host "`n按回车退出"
