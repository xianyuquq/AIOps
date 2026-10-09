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

# 注意：这里用 Continue 而不是 Stop。
# 本脚本要反复调用外部程序（python / pip / npm），它们把诊断信息写到 stderr 是常态
# —— 例如「依赖还没装」时 import 探测就会输出 traceback。
# 在 Stop 模式下这些 stderr 会被当成终止性错误，脚本还没开始安装就退出了。
# 因此改为 Continue，并用 $LASTEXITCODE 显式判断每一步的成败。
$ErrorActionPreference = "Continue"
$Root   = $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Req    = Join-Path $Root "backend\requirements.txt"
$Port   = if ($env:AIOPS_PORT) { $env:AIOPS_PORT } else { 8848 }

function Info($m)  { Write-Host "  $m" }
function Ok($m)    { Write-Host "  $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  $m" -ForegroundColor Yellow }
function Fail($m)  { Write-Host ""; Write-Host "  [错误] $m" -ForegroundColor Red; Write-Host "" }

# ---------- 找一个**真正可用**的 Python ----------
# 坑：Windows 自带的 python.exe / python3.exe 常是 Microsoft Store 的占位符，
# 执行它不会运行 Python，只会弹应用商店。必须实际验证过才算数。
function Find-Python {
    $cands = @()

    # 1) py 启动器最可靠
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $cands += [pscustomobject]@{ Exe = $py.Source; Args = @('-3') } }

    # 2) PATH 上的 python / python3（排除 WindowsApps 占位符）
    foreach ($n in @('python', 'python3')) {
        foreach ($c in (Get-Command $n -All -ErrorAction SilentlyContinue)) {
            if ($c.Source -match '\\WindowsApps\\') { continue }
            $cands += [pscustomobject]@{ Exe = $c.Source; Args = @() }
        }
    }

    # 3) 常见安装目录
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        'C:\'
    ) | Where-Object { $_ -and (Test-Path $_) }
    foreach ($r in $roots) {
        Get-ChildItem $r -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
            ForEach-Object {
                $exe = Join-Path $_.FullName 'python.exe'
                if (Test-Path $exe) { $cands += [pscustomobject]@{ Exe = $exe; Args = @() } }
            }
    }

    # 逐个验证：必须真的能跑出版本号
    foreach ($c in $cands) {
        try {
            $v = & $c.Exe @($c.Args) -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$v" -match '^3\.(\d+)$') {
                if ([int]$Matches[1] -ge 10) { return $c }
                Warn "跳过 Python 3.$($Matches[1])（需要 3.10+）：$($c.Exe)"
            }
        } catch { }
    }
    return $null
}

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
    $pyc = Find-Python
    if (-not $pyc) {
        Fail "没有找到可用的 Python 3.10+。"
        Info "注意：Windows 自带的 python.exe 常是应用商店占位符，不能用。"
        Info "请安装 Python（勾选 Add to PATH）： https://www.python.org/downloads/"
        Info "或用 winget 安装： winget install --id Python.Python.3.12 -e"
        Info "若已安装但仍报此错，请在「设置 → 应用 → 高级应用设置 →"
        Info "应用执行别名」中关闭 python.exe / python3.exe 的商店别名。"
        exit 1
    }
    Info "使用 Python：$($pyc.Exe)"
    & $pyc.Exe @($pyc.Args) -m venv (Join-Path $Root ".venv")
    if (-not (Test-Path $Python)) {
        Fail "虚拟环境创建失败。请手动执行： `"$($pyc.Exe)`" -m venv .venv"
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
        # 用 import 探测：比比对版本号更快也更可靠。
        # stderr 丢弃，只看退出码（缺包时 Python 会输出 traceback）。
        $null = & $Python -c "import fastapi, uvicorn, sqlalchemy, asyncssh, bcrypt, jose, cryptography, apscheduler, httpx, pydantic_settings" 2>$null
        if ($LASTEXITCODE -ne 0) { $needInstall = $true }
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
    Fail "端口 $Port 已被 PID $owner 占用（多半是上一次没停干净）。"
    Info "停止它：.\stop.cmd"
    Info "换端口：`$env:AIOPS_PORT=9000; .\start.cmd"
    Info "或手动结束：Stop-Process -Id $owner -Force"
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
