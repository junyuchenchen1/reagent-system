# 检验科试剂管理系统 - 卸载脚本
# 仅移除自启/计划任务/快捷方式，不删除项目文件和数据

$proj = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "停止服务..." -ForegroundColor Yellow
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run.py*' } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "删除计划任务..." -ForegroundColor Yellow
Unregister-ScheduledTask -TaskName "ReagentServer_Watchdog" -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "ReagentServer_Backup" -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "删除启动文件夹快捷方式..." -ForegroundColor Yellow
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "试剂管理系统.lnk"
Remove-Item $lnk -ErrorAction SilentlyContinue

Write-Host "删除桌面快捷方式..." -ForegroundColor Yellow
$desktop = [Environment]::GetFolderPath("Desktop")
Remove-Item (Join-Path $desktop "试剂管理系统.lnk") -ErrorAction SilentlyContinue
Remove-Item (Join-Path $desktop "停止试剂管理系统.lnk") -ErrorAction SilentlyContinue

Write-Host "`n卸载完成。项目文件和数据库保留在: $proj" -ForegroundColor Green
Write-Host "如需彻底删除，请手动删除该文件夹。" -ForegroundColor Green
Read-Host "按回车退出"
