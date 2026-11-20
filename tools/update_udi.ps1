param(
    [Parameter(Position = 0)][string]$Package
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Pause-Exit([int]$code) {
    Read-Host "按回车关闭"
    exit $code
}

if (-not $Package -or $Package -eq "/?") {
    Write-Host "用法：把下载的 UDI 发布包（ZIP）拖到 update_udi.bat 上，或"
    Write-Host '      update_udi.bat "D:\下载\UDID_MONTH_UPDATE_202610.zip"'
    Write-Host ""
    Write-Host "发布包下载：https://udi.nmpa.gov.cn/download.html（每日包 / 每周包 / 每月包 / 全量包）"
    Write-Host "建议：先导一次全量包补历史，之后每月导一次月度包。"
    Pause-Exit 0
}

if (-not (Test-Path -LiteralPath $Package)) {
    Write-Host "[错误] 找不到文件：$Package" -ForegroundColor Red
    Pause-Exit 1
}
$Package = (Resolve-Path -LiteralPath $Package).Path

# 优先用本机 Python 3.14（装 flask/sqlalchemy/pymysql）；PATH 里的 python 可能是别的运行时
$py = "E:\Python\Python314\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source } else {
        Write-Host "[错误] 没找到 python，请确认 Python 3.14 装在 E:\Python\Python314" -ForegroundColor Red
        Pause-Exit 1
    }
}

Write-Host "============================================================"
Write-Host " UDI 产品库更新"
Write-Host " 发布包：$Package"
Write-Host "============================================================"
Write-Host ""

Write-Host "[1/3] 备份数据库（出问题可用 restore.py 回滚）..." -ForegroundColor Cyan
& $py backup.py
Write-Host ""

Write-Host "[2/3] 预检：只算这个包能命中多少条检验试剂，不写库..." -ForegroundColor Cyan
& $py udi_import.py $Package --dry-run
Write-Host ""

$go = Read-Host "确认无误按回车开始正式导入（输入 n 取消）"
if ($go -eq "n") {
    Write-Host "已取消，未做任何写入。"
    Pause-Exit 0
}

Write-Host "[3/3] 正式导入..." -ForegroundColor Cyan
& $py udi_import.py $Package
Write-Host ""

Write-Host "当前 UDI 产品库条数：" -NoNewline
& $py -c "import sqlite3;c=sqlite3.connect('instance/reagent.db');print(c.execute('select count(*) from udi_product').fetchone()[0])"
Write-Host ""
Write-Host "完成。明细见 logs\udi_import.log，服务不用重启，页面刷新后即可扫到新产品。"
Pause-Exit 0
