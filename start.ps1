# ============================================================
#  AI 智能运维平台 —— 一键启动
#
#  用法：在本文件所在目录打开 PowerShell，执行
#        .\start.ps1
#
#  可选：自定义端口
#        $env:AIOPS_PORT=9000; .\start.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$Root   = $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Port   = if ($env:AIOPS_PORT) { $env:AIOPS_PORT } else { 8848 }

# ---------- 检查虚拟环境 ----------
if (-not (Test-Path $Python)) {
    Write-Host ""
    Write-Host "  [错误] 未找到虚拟环境：$Python" -ForegroundColor Red
    Write-Host ""
    Write-Host "  请先在 $Root 目录下执行：" -ForegroundColor Yellow
    Write-Host "    python -m venv .venv"
    Write-Host "    .\.venv\Scripts\python.exe -m pip install fastapi `"uvicorn[standard]`" sqlalchemy pydantic pydantic-settings asyncssh bcrypt `"python-jose[cryptography]`" cryptography python-multipart apscheduler httpx"
    Write-Host ""
    exit 1
}

# ---------- 检查端口占用 ----------
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = $busy | Select-Object -First 1 -ExpandProperty OwningProcess
    Write-Host ""
    Write-Host "  [错误] 端口 $Port 已被 PID $owner 占用。" -ForegroundColor Red
    Write-Host "         换端口：`$env:AIOPS_PORT=9000; .\start.ps1" -ForegroundColor Yellow
    Write-Host "         或结束占用进程：Stop-Process -Id $owner -Force" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# ---------- 检查前端产物 ----------
$dist = Join-Path $Root "frontend\dist\index.html"
if (-not (Test-Path $dist)) {
    Write-Host ""
    Write-Host "  [提示] 前端尚未构建，将只提供 API 接口。" -ForegroundColor Yellow
    Write-Host "         如需界面，请先执行：cd frontend; npm install; npm run build" -ForegroundColor Yellow
    Write-Host ""
}

# ---------- 中文不乱码 ----------
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║        AI 智能运维平台  已启动               ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""
Write-Host "   访问地址 : http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "   接口文档 : http://127.0.0.1:$Port/docs" -ForegroundColor Green
Write-Host "   默认账号 : admin / admin123" -ForegroundColor Gray
Write-Host ""
Write-Host "   停止服务 : 按 Ctrl+C" -ForegroundColor Gray
Write-Host ""

# ---------- 启动 ----------
# 注意：必须单进程运行。SSH 连接池与终端会话是进程内状态，
#       加 --workers 会导致终端归属混乱。
Set-Location (Join-Path $Root "backend")
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
