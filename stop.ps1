# ============================================================
#  AI 智能运维平台 —— 停止
#
#  用法：.\stop.cmd            停止（推荐，避免执行策略拦截）
#        .\stop.ps1           同上，PowerShell 里直接用
#        .\stop.ps1 -DryRun   只看会停掉哪些进程，不真的停
#        $env:AIOPS_PORT=9000; .\stop.ps1    停止指定端口的实例
#
#  为什么按端口找而不是按进程名找：
#    本机可能同时跑着 mock SSH 服务器、造数据脚本等其它 python 进程，
#    「杀掉所有 python」会误伤它们。按监听端口定位才精确。
# ============================================================

param(
    [int]$Port = 0,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"

if ($Port -le 0) {
    $Port = if ($env:AIOPS_PORT) { [int]$env:AIOPS_PORT } else { 8848 }
}

function Info($m) { Write-Host "  $m" }
function Ok($m)   { Write-Host "  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  $m" -ForegroundColor Yellow }

# ---------- 找出监听该端口的进程 ----------
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue

if (-not $listeners) {
    Info "端口 $Port 上没有在运行的服务（无需停止）。"
    exit 0
}

$rootPids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique

# ---------- 连同子进程一起收集 ----------
# uvicorn 可能派生子进程；只杀父进程会留下孤儿继续占着端口。
function Get-Descendants([int]$Parent) {
    $kids = Get-CimInstance Win32_Process -Filter "ParentProcessId=$Parent" -ErrorAction SilentlyContinue
    foreach ($k in $kids) {
        $k.ProcessId
        Get-Descendants -Parent $k.ProcessId
    }
}

$targets = @()

# 祖先：监听 socket 可能被子进程持有，只杀子进程则父进程还活着。
# 只向上追溯到「仍是 uvicorn 的 python 进程」为止，避免误伤启动它的窗口。
function Test-IsPlatformProcess([int]$P) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$P" -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    if ($proc.Name -notmatch '^python') { return $false }
    return ($proc.CommandLine -like '*uvicorn*app.main*')
}

function Get-Ancestors([int]$Child) {
    $result = @()
    $cur = $Child
    for ($i = 0; $i -lt 6; $i++) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
        if (-not $proc) { break }
        $parent = $proc.ParentProcessId
        if (-not $parent -or $parent -eq 0) { break }
        if (-not (Test-IsPlatformProcess -P $parent)) { break }
        $result += $parent
        $cur = $parent
    }
    return $result
}

foreach ($p in $rootPids) {
    $targets += $p
    $targets += Get-Descendants -Parent $p
    $targets += Get-Ancestors -Child $p
}
$targets = $targets | Select-Object -Unique

Info "端口 $Port 上的服务："
foreach ($p in $targets) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue
    if ($proc) {
        $cmd = $proc.CommandLine
        if ($cmd -and $cmd.Length -gt 96) { $cmd = $cmd.Substring(0, 96) + "…" }
        Info ("  PID {0,-7} {1}" -f $p, $cmd)
    }
}

if ($DryRun) {
    Warn "-DryRun：以上进程**未**被停止。去掉该参数即真正执行。"
    exit 0
}

# ---------- 先子后父 ----------
Info ""
$failed = 0
foreach ($p in ($targets | Sort-Object -Descending)) {
    try {
        Stop-Process -Id $p -Force -ErrorAction Stop
        Ok "已停止 PID $p"
    } catch {
        Warn "停止 PID $p 失败：$($_.Exception.Message)"
        $failed++
    }
}

# ---------- 确认端口已释放 ----------
Start-Sleep -Seconds 2
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
Info ""
if ($still) {
    Warn "端口 $Port 仍被占用（PID $($still[0].OwningProcess)），可能需要管理员权限。"
    exit 1
}
Ok "端口 $Port 已释放，服务已停止。"
exit 0
