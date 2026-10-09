# ============================================================
#  AI 智能运维平台 —— 一键启动
#
#  用法：在本文件所在目录打开 PowerShell，执行
#        .\start.ps1
#
#  可选：
#        $env:AIOPS_PORT=9000; .\start.ps1     换端口
#        .\start.ps1 -SkipInstall              跳过依赖自动安装
#        .\start.ps1 -SkipFrontend             跳过前端构建
#
#  从 GitHub 克隆下来后直接跑即可：
#  .venv、node_modules、frontend\dist 都不在仓库里（体积大/含本机状态），
#  脚本会自动补齐。
# ============================================================

param(
    [switch]$SkipInstall,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$Root   = $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Req    = Join-Path $Root "backend\requirements.txt"
$Port   = if ($env:AIOPS_PORT) { $env:AIOPS_PORT } else { 8848 }

function Info($m)  { Write-Host "  $m" }
function Ok($m)    { Write-Host "  $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  $m" -ForegroundColor Yellow }
function Fail($m)  { Write-Host ""; Write-Host "  [错误] $m" -ForegroundColor Red; Write-Host "" }

# ---------- 控制台设为 UTF-8 ----------
# 不设的话 Python 输出的中文日志会变成乱码（控制台默认是 GBK/936）。
# 必须在启动 Python 之前设置。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8       = "1"
} catch {
    Warn "控制台编码设置失败，中文日志可能显示为乱码（不影响功能）"
}

# ---------- 1. 虚拟环境（缺失则自动创建） ----------
if (-not (Test-Path $Python)) {
    if ($SkipInstall) {
        Fail "未找到虚拟环境：$Python`n         已指定 -SkipInstall，请先自行创建。"
        exit 1
    }

    Info "未找到虚拟环境，正在创建（首次运行需要几分钟）…"
    $sysPy = $null
    foreach ($cand in @("python", "py")) {
        $c = Get-Command $cand -ErrorAction SilentlyContinue
        if ($c) { $sysPy = $c.Source; break }
    }
    if (-not $sysPy) {
        Fail "没有找到 python。请先安装 Python 3.10+ 并加入 PATH。`n         下载：https://www.python.org/downloads/"
        exit 1
    }
    Info "使用 $sysPy"
    & $sysPy -m venv (Join-Path $Root ".venv")
    if (-not (Test-Path $Python)) {
        Fail "虚拟环境创建失败。请手动执行：python -m venv .venv"
        exit 1
    }
    Ok "虚拟环境已创建"
}

# ---------- 2. 后端依赖（缺失则自动安装） ----------
if (-not $SkipInstall) {
    $needInstall = $false
    if (-not (Test-Path $Req)) {
        Warn "未找到 backend\requirements.txt，跳过依赖检查"
    } else {
        # 用 import 探测：比比对版本号更快也更可靠
        $probe = & $Python -c "import fastapi, uvicorn, sqlalchemy, asyncssh, bcrypt, jose, cryptography, apscheduler, httpx, pydantic_settings; print('ok')" 2>&1
        if ($LASTEXITCODE -ne 0 -or "$probe" -notmatch "ok") {
            $needInstall = $true
        }
    }

    if ($needInstall) {
        Info "后端依赖不完整，正在安装（首次运行需要几分钟）…"
        & $Python -m pip install --upgrade pip --quiet
        & $Python -m pip install -r $Req
        if ($LASTEXITCODE -ne 0) {
            Fail "依赖安装失败。请手动执行：`n         .\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt"
            exit 1
        }
        Ok "后端依赖已安装"
    }
}

# ---------- 3. 端口占用 ----------
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = $busy | Select-Object -First 1 -ExpandProperty OwningProcess
    Fail "端口 $Port 已被 PID $owner 占用。"
    Info "换端口：`$env:AIOPS_PORT=9000; .\start.ps1"
    Info "或结束占用进程：Stop-Process -Id $owner -Force"
    exit 1
}

# ---------- 4. 前端（缺失则构建） ----------
$dist = Join-Path $Root "frontend\dist\index.html"
if (-not (Test-Path $dist)) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($SkipFrontend -or -not $npm) {
        Warn "前端尚未构建，将只提供 API 接口。"
        if (-not $npm) { Warn "未检测到 npm，无法自动构建。请安装 Node.js 18+。" }
        Warn "如需界面：cd frontend; npm install; npm run build"
    } else {
        Info "前端尚未构建，正在安装依赖并打包（首次运行需要几分钟）…"
        Push-Location (Join-Path $Root "frontend")
        try {
            if (-not (Test-Path "node_modules")) {
                & npm install
                if ($LASTEXITCODE -ne 0) { throw "npm install 失败" }
            }
            & npm run build
            if ($LASTEXITCODE -ne 0) { throw "npm run build 失败" }
            Ok "前端已构建"
        } catch {
            Warn "前端构建失败：$_"
            Warn "后端仍会启动，但界面不可用。可稍后手动执行：cd frontend; npm install; npm run build"
        } finally {
            Pop-Location
        }
    }
}

# ---------- 启动 ----------
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

# 注意：必须单进程运行。SSH 连接池与终端会话是进程内状态，
#       加 --workers 会导致终端归属混乱。
Set-Location (Join-Path $Root "backend")
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
