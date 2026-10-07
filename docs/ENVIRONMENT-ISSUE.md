# 环境诊断报告（阻塞项）

> 记录时间：本会话 · 状态：**已解决**

## ✅ 结论（更新）

**根因：DSH 受限沙箱模式。** 在受限沙箱下任何子进程创建都以
`0xC0000142` 失败；把本会话切换到**完全权限**（danger-full-access）后，
`pwsh`、`cmd`、Python、Node **全部立即恢复正常**。

同一会话内的对照实验：

| 模式 | 命令 | 结果 |
|---|---|---|
| 受限沙箱 | `Write-Output "test"` | `0xC0000142` |
| 受限沙箱 | `cmd /c echo probe` | `0xC0000142` |
| 受限沙箱 | `python.exe -c "print(1)"` | `0xC0000142` |
| **完全权限** | `Get-ChildItem Env:` | ✅ 正常输出 |
| **完全权限** | `python.exe -c "import sys; print(sys.version)"` | ✅ `3.12.14` |

**重要修正**：下面的原始报告**错误地排除了沙箱因素**。当时的推理是
"沙箱拒绝会报 `[sandbox: file access denied]`，而这里是进程崩溃，所以不是沙箱问题"
——这个推理不成立：沙箱对**子进程创建**的拦截表现为 `0xC0000142`，
而不是文件访问类错误。原文保留，用于记录这次误判。

**教训**：当"所有可执行文件都无法启动"时，除了系统环境损坏，
应把**执行沙箱**也列入嫌疑，并尽早做"切换权限模式后重试"的对照实验，
而不是先投入大量探测去推断系统级原因。

---

## 原始诊断记录（保留，含上述误判）

## 现象

在本机执行**任何**子进程都会失败，返回退出码 `3221225794`（十六进制 `0xC0000142`，`STATUS_DLL_INIT_FAILED`）。

已尝试的全部方式，**无一例外失败**：

| # | 尝试内容 | 结果 |
|---|---|---|
| 1 | `Get-Location` | `0xC0000142` |
| 2 | `Get-ChildItem -Force -Name` | `0xC0000142` |
| 3 | `node -v` / `pnpm -v` / `npm -v` / `python --version` / `go version` | `0xC0000142` |
| 4 | 绝对路径调用 `...\dependencies\node\bin\node.exe -v` | `0xC0000142` |
| 5 | `cmd /c "echo hello"` | `0xC0000142` |
| 6 | `cmd /c "dir C:\Windows\System32\kernel32.dll"` | `0xC0000142` |
| 7 | `Write-Output "test"`（最小命令） | `0xC0000142` |
| 8 | `$env:PATH`（纯变量展开，无子进程） | `0xC0000142` |
| 9 | `Get-Date` | `0xC0000142` |
| 10 | `$PSVersionTable.PSVersion` | `0xC0000142` |
| 11 | `Write-Host "probe"` / `"probe2"` | `0xC0000142` |

## 关键判据

1. **不是 DSH 沙箱拦截。** 沙箱拒绝会明确报 `[sandbox: file access denied under <mode> mode]`，而这里连 `Write-Output` 这种不触碰文件系统的命令都失败。当前策略为 `workspace-write`，文件读写工具全部正常（本会话已成功创建多个文件）。
2. **失败发生在进程启动之前。** `0xC0000142` 表示进程加载 DLL 阶段就崩溃，命令体根本没被执行——所以这不是命令写错，也不是权限问题。
3. **波及所有可执行文件**，包括 `pwsh`、`cmd`、Python、Node，说明是系统级或会话级的加载环境损坏，而非某个工具损坏。

## 最可能的原因（按可能性排序）

1. **用户/系统环境变量被破坏**——尤其是 `PATH`、`SystemRoot`、`TEMP`、`ComSpec`、`windir`。这些变量一旦被清空或指向不存在的路径，进程在解析依赖 DLL 时会直接失败。第 8 项尝试（读取 `$env:PATH` 本身也崩）高度指向这一条。
2. **安全软件 / EDR 拦截进程创建**——某些策略会注入 DLL 并在失败时导致 `0xC0000142`。
3. **系统 DLL 损坏或 Windows 更新失败**。

## 建议的修复步骤（需要你在图形界面或可用终端中操作）

```powershell
# 1) 检查关键环境变量是否完好（应有正常值，不应为空）
echo $env:SystemRoot
echo $env:PATH
echo $env:TEMP
echo $env:ComSpec

# 2) 若 SystemRoot 为空，手动临时修复后重试
$env:SystemRoot = "C:\Windows"
$env:PATH = "C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem"

# 3) 检查系统文件完整性（需管理员）
sfc /scannow
DISM /Online /Cleanup-Image /RestoreHealth

# 4) 如怀疑安全软件，临时停用后重试
```

**注意**：如果 `PATH` 或 `SystemRoot` 真的被破坏，重启资源管理器或重新登录可能就能恢复；这类问题的典型特征是"昨天还好好的，今天所有命令行都打不开"。

## 对交付的影响

| 影响项 | 说明 |
|---|---|
| ✅ 不受影响 | 文件读写、代码编写、文档产出、纯静态审阅 |
| ❌ 受影响 | 运行 Python/Node、`pip install`、`npm install`、编译校验、启动服务、端到端自测 |

因此本项目的代码**无法在本机做任何编译或运行验证**。这一点必须在交付说明中明确标注，避免造成"已验证可运行"的错误印象。

## 附带发现：本机运行时依赖情况

- Python 运行时存在（3.13 系列），但 **`site-packages` 中没有任何 Web 相关依赖**：无 `fastapi`、`uvicorn`、`sqlalchemy`、`pydantic`、`asyncssh`、`paramiko`、`psutil`、`alembic`。
- 仅有的第三方包是文档/数据类：`numpy`、`pandas`、`python-docx`、`python-pptx`、`openpyxl`、`Pillow`、`lxml`、`XlsxWriter`。
- Node 运行时存在，但 `node_modules` 为空。
- 标准库能力完整（`sqlite3.dll`、`_ssl.pyd`、`_socket.pyd`、`_asyncio.pyd`、`_hashlib.pyd`、`_uuid.pyd` 均在），**这意味着即使装不上第三方包，也能用标准库跑通一套可用的运维核心**。

---

**结论**：环境阻塞项属于操作系统层面，我无法在本会话内修复。代码将按"设计为可独立验证的核心 + 明确标注未运行验证"的方式交付。
