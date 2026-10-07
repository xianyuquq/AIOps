# ============================================================
#  演示模式 —— 一键体验「多终端同步广播」
#
#  会做四件事：
#    1. 新窗口启动 4 个模拟 Linux 节点（端口 2501~2504）
#    2. 新窗口启动平台服务（端口 8848）
#    3. 把这 4 个节点注册进平台
#    4. 打开浏览器
#
#  体验方式：登录后进入「多终端」→ 勾选 4 台 web-mock-* →
#            建立会话 → 在任意终端输入 df -Th 并回车，
#            可以看到四台同时显示、同时执行。
#
#  注意：模拟节点仅用于试用，真实使用请在「服务器管理」中
#        添加你自己的服务器。
# ============================================================

$ErrorActionPreference = "Stop"
$Root   = $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Port   = if ($env:AIOPS_PORT) { $env:AIOPS_PORT } else { 8848 }

if (-not (Test-Path $Python)) {
    Write-Host "  [错误] 未找到虚拟环境：$Python" -ForegroundColor Red
    Write-Host "  请先执行 .\start.ps1 查看安装说明。" -ForegroundColor Yellow
    exit 1
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"

Write-Host ""
Write-Host "  [1/4] 启动 4 个模拟 SSH 节点（2501~2504）…" -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$Host.UI.RawUI.WindowTitle='AIOps 模拟节点'; & '$Python' '$Root\backend\tests\mock_ssh_servers.py'"
)
Start-Sleep -Seconds 4

Write-Host "  [2/4] 启动平台服务（$Port）…" -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$Host.UI.RawUI.WindowTitle='AIOps 平台服务'; & '$Python' -m uvicorn app.main:app --host 127.0.0.1 --port $Port"
)

# 等待服务就绪
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 800
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
        if ($r.status -eq "ok") { $ready = $true; break }
    } catch { }
}

if (-not $ready) {
    Write-Host "  [错误] 平台启动超时，请查看「AIOps 平台服务」窗口的报错。" -ForegroundColor Red
    exit 1
}
Write-Host "        平台已就绪。" -ForegroundColor Green

Write-Host "  [3/4] 注册模拟节点到平台…" -ForegroundColor Cyan
Push-Location (Join-Path $Root "backend")
try {
    & $Python "tests\seed_mock_servers.py"
} finally {
    Pop-Location
}

Write-Host "  [4/4] 打开浏览器…" -ForegroundColor Cyan
Start-Process "http://127.0.0.1:$Port"

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║   演示环境已就绪                             ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""
Write-Host "   账号：admin / admin123" -ForegroundColor Gray
Write-Host "   试用：多终端 → 勾选 4 台 web-mock-* → 建立会话 → 输入 df -Th 回车" -ForegroundColor Gray
Write-Host ""
Write-Host "   关闭：在「AIOps 模拟节点」和「AIOps 平台服务」" -ForegroundColor Gray
Write-Host "         两个窗口分别按 Ctrl+C" -ForegroundColor Gray
Write-Host ""
