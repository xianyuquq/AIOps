# 验证报告

---

## 概览

| 层级 | 验证内容 | 用例数 | 结果 |
|---|---|---|---|
| L1 单元/集成 | 解析器、沙箱、告警逻辑、加密、广播语义、命令拦截 | 56 | ✅ 全通过 |
| L2 真实 SSH | asyncssh mock 服务器，走完整 SSH 链路（含 SFTP） | 11 | ✅ 全通过 |
| **L2b 脚本管理** | 内置脚本启用/停用、运行参数、复制、采集任务注册 | 17 | ✅ 全通过 |
| **L2c 数据库与备份** | 实例增改删、部分更新、凭据保护、备份任务、级联删除 | 20 | ✅ 全通过 |
| L3 真实浏览器 | Playwright + Chromium，含 4 台实时广播与错误显示 | 114 | ✅ 全通过 |
| **L4 真实服务器** | **银河麒麟 Kylin V10 实机：连接、命令执行、指标采集** | — | ✅ 通过 |
| **L5 真实数据库** | **Docker 中真实 PostgreSQL 16 与 MySQL 8 的指标采集** | — | ✅ 通过 |
| **L6 备份链路** | 模板渲染、密码注入、产物识别、保留期清理、执行历史 | — | ✅ 通过 |
| **L7 真实 Kingbase** | **真实 KingbaseES V009R001C010（麒麟测试机）直连 + 跳板采集** | — | ✅ 通过 |
| **L8 大盘可见性** | **数据库指标在总览大盘与列表页的呈现** | 27 | ✅ 全通过 |
| **L9 趋势预测** | **线性拟合、两类指标模式、低可信度抑制** | 25 | ✅ 全通过 |
| **L10 真实备份** | **真实 sys_dump 产出 586KB 归档并用 sys_restore 校验** | — | ✅ 通过 |

> 后端合计 **161 个用例**（`pytest tests/`）全部通过；
> 浏览器端 **7 套共 141 项**全部通过。

> 测试隔离：`tests/conftest.py` 会把数据库指向临时目录，
> 因此跑测试**不会**读写真实的 `data/aiops.db`，也不会覆盖真实的 `master.key`。

---

## L1：单元与集成测试

```bash
cd backend
../.venv/Scripts/python.exe -m pytest tests/test_core.py tests/test_terminal_broadcast.py -v
```

**覆盖范围**：

- **脚本解析器**：`line_kv` / `json`（含 JSONL 逐行形态）/ `prometheus` 三种格式；
  注释与畸形行忽略；未声明的 label 被过滤
- **Python 沙箱**：`ctx.emit()` 产出指标、**死循环脚本被超时强杀**、
  异常捕获、入口函数缺失报错
- **告警逻辑**：6 种比较运算符、**指纹聚合**（同规则+指标+标签产生相同指纹；
  不同维度产生不同指纹；label 顺序无关）
- **凭据加密**：AES-GCM 往返、**相同明文产生不同密文**（nonce 不复用）、
  超 72 字节密码安全处理
- **广播终端语义**（用 FakeExecutor）：一次广播四台全收到、回车同步、
  逐字符序列四台一致、镜像模式关闭回显、单台失败不影响其他、
  单机模式只写目标终端、命令审计记录、模式切换补发 `stty -echo`
- **危险命令拦截**：`rm -rf /`、`mkfs`、`dd of=/dev/sda`、fork 炸弹、
  `shutdown` 等被拦截；`df -Th`、`ls`、`systemctl status` 正常放行

**重要发现**：沙箱**不能**阻止脚本访问操作系统
（`os.system` 会成功执行）。测试中明确记录了这一边界，
避免产生"沙箱等于安全隔离"的错误认知。

---

## L2：真实 SSH 广播测试

```bash
cd backend
../.venv/Scripts/python.exe -m pytest tests/test_ssh_broadcast_e2e.py -v -s
```

**方法**：用 asyncssh 在本机启动 4 个「假 Linux 服务器」（各监听不同端口，
模拟 shell 行为），然后让平台的**真实** `SSHConnectionPool` 与 `TerminalHub`
连接它们。**不使用任何 mock 替换 SSH 层。**

**验证项**：

| 测试 | 验证内容 |
|---|---|
| `test_broadcast_reaches_all_mock_ssh_servers` | 一次广播，4 台真实 SSH 会话都执行并返回输出 |
| `test_each_server_returns_its_own_identity` | 输出按 server_id 正确分流（各返回自身主机名） |
| `test_platform_disables_remote_echo_on_session_setup` | 平台确实向每个通道下发 `stty -echo` |
| `test_no_duplicate_echo_when_remote_echo_off` | 命令文本不在远端输出中出现（无重复显示） |
| `test_broadcast_latency_is_low` | 广播到 4 台 < 500ms（实测 ~0ms） |
| `test_unknown_command_returns_shell_error` | 错误命令输出正确回传 |
| `test_single_mode_does_not_broadcast` | 单机模式只写目标终端 |
| `test_sftp_list_dir` | SFTP 列目录，且过滤 `.` 与 `..` |
| `test_sftp_upload_and_download_roundtrip` | 上传后下载，字节级一致 |
| `test_sftp_upload_parallel_to_multiple_servers` | 并行上传到多台服务器 |

**实测输出**：

```
各终端输出长度： {1: 181, 2: 181, 3: 181, 4: 181}   ← 四台完全一致
广播到 4 台耗时：0.0ms                              ← 并行写入
10 passed
```

---

## L2b：脚本管理的回归测试

背景是一个真实缺陷：后端的 `PATCH /scripts/{id}` 原本拒绝**所有**对内置脚本的
修改，包括「启用/停用」。而内置脚本默认停用，于是用户**永远无法启用内置监控脚本**——
界面上点「启用」只会得到 HTTP 400，且提示语为"请复制一份后再编辑"，
但当时界面上**并没有复制功能**。

覆盖的行为：

| 用例 | 锁定内容 |
|---|---|
| `test_builtin_scripts_start_disabled` | 内置脚本默认停用 |
| `test_enable_builtin_script_via_minimal_payload` | ★ 只发 `{enabled:true}` 即可启用 |
| `test_toggle_with_full_payload_echo_is_accepted` | ★ 整对象原样回传不应被误判为改内容 |
| `test_can_change_interval_and_timeout` | 采集周期/超时可改 |
| `test_can_change_target_scope` | 作用域可改 |
| `test_cannot_change_builtin_content`（5 个参数化） | 正文/名称/运行时/平台/分类不可改 |
| `test_cannot_change_emit_schema` | 解析规则不可改 |
| `test_cannot_change_builtin_content` 断言提示语 | 错误提示必须说明原因并指向「复制」 |
| `test_duplicate_creates_editable_copy` | ★ 副本可编辑、可启用 |
| `test_duplicate_names_do_not_collide` | 多次复制不重名 |
| `test_cannot_delete_builtin_script` | 内置脚本不可删除 |
| `test_enabling_registers_collection_job` | ★ 启用后**真的注册了采集任务**（否则只是改了个标记） |

浏览器侧回归见 `frontend/tests/e2e_scripts.mjs`（17 项），
覆盖真实点击「启用」按钮、只读提示、试运行可用、复制副本等交互。

---

## L3：真实浏览器端到端测试

需要三个进程：

```bash
# 1) 启动 4 个 mock SSH 节点（端口 2501~2504）
cd backend && ../.venv/Scripts/python.exe tests/mock_ssh_servers.py

# 2) 启动平台服务
cd backend && ../.venv/Scripts/python.exe -m uvicorn app.main:app --port 8848

# 3) 注册 mock 节点到平台
cd backend && ../.venv/Scripts/python.exe tests/seed_mock_servers.py
```

然后运行浏览器测试：

```bash
cd frontend
npm install -D playwright && npx playwright install chromium
node tests/e2e_terminal.mjs      # 核心广播测试
node tests/e2e_browser.mjs       # 页面渲染与登录测试
```

**`e2e_terminal.mjs` 验证项（真实按键输入）**：

| 检查项 | 结果 |
|---|---|
| 登录成功 | ✅ |
| 选中 4 台 mock 服务器 | ✅ |
| 创建 4 个终端面板 | ✅ |
| 4 台终端均就绪（ready） | ✅ |
| ★ 逐字符输入实时同步到全部 4 个面板 | ✅ |
| ★ 回车后 4 台同时执行并各自返回结果 | ✅ |
| 4 台命令输出完全一致（结果同步） | ✅ |
| 各终端显示自身主机名（输出正确分流） | ✅ |
| 命令输入栏同样广播到全部终端 | ✅ |
| 危险命令被拦截并提示 | ✅ |
| 命令写入审计日志 | ✅ |
| 无浏览器运行时错误 | ✅ |

**`e2e_browser.mjs` 验证项**：登录页 → 大盘 → 8 个功能页面渲染 →
多终端页面 → AI 降级提示 → 控制台零错误，共 **19 项全通过**。

SFTP 文件浏览已通过平台 API 与实际 mock SFTP 服务验证：

```
=== SFTP 列目录 server_id=1 ===
  [DIR] data
  [DIR] logs
        app.log  1460B
        config.yaml  37B
        README.txt  77B
```

截图保存在 `docs/screenshots/`，其中 `12-broadcast-result.png`
直观展示了四台终端同步执行 `df -Th` 的结果。

---

## L4：真实麒麟服务器验证

这一层使用用户的真实内网服务器，是最有说服力的验证。

### 目标环境

```
NAME="Kylin Linux Advanced Server"      # 银河麒麟高级服务器版
VERSION="V10 (Halberd)"
kernel: 4.19.90-89.11.v2401.ky10.x86_64
LANG=zh_CN.UTF-8                        # 中文 locale
filesystem: /dev/mapper/klas-root  xfs
```

### 验证结果

| 项目 | 结果 |
|---|---|
| TCP 连通 | ✅ |
| SSH 服务标识 | ✅ `SSH-2.0-OpenSSH_8.2` |
| 握手与认证（root + 密码） | ✅ 认证成功 |
| 连接测试耗时 | ✅ **61ms** |
| 远程命令执行 | ✅ `uname -a` / `df -Th` 正常返回 |
| 内置 Linux 采集脚本 | ✅ **34 个指标，109ms 完成** |

**中文 locale 下的解析验证**（这是国产化环境最常见的坑）：

`df` 的输出被翻译成中文（`文件系统 类型 容量 已用 可用 已用% 挂载点`），
但采集脚本仍正确解析出挂载点与使用率：

```
磁盘明细：
   /                        3.0%
   /boot                    21.0%
```

之所以不受影响，是因为脚本设计上避开了对表头文本的依赖：
- `df -PT` 使用 POSIX 输出格式（列位置稳定），并用 `NR>1` 跳过表头
- 内存取自 `/proc/meminfo`，与 locale 无关
- 网络/磁盘 IO 取自 `/proc/net/dev` 与 `/proc/diskstats`，与 locale 无关

复现：

```powershell
cd E:\Agent\backend
..\.venv\Scripts\python.exe tests\verify_builtin_on_real.py <服务器ID>
```

---

## L5：真实数据库采集验证

此前「数据库监控」只有界面与模型，**没有任何代码真正连上数据库**——
`runtime == "sql"` 的脚本会被当作 shell 命令通过 SSH 执行，必然失败。
本层验证修复后的真实采集能力。

### 环境

用 Docker 启动两个真实数据库（Docker Hub 直连不通，改用可达的镜像源）：

```powershell
docker run -d --name aiops-pg -e POSTGRES_PASSWORD=<本地测试口令> -e POSTGRES_DB=testdb `
  -p 5432:5432 docker.m.daocloud.io/library/postgres:16-alpine
docker run -d --name aiops-mysql -e MYSQL_ROOT_PASSWORD=<本地测试口令> -e MYSQL_DATABASE=testdb `
  -p 3306:3306 docker.m.daocloud.io/library/mysql:8
```

### 结果

```powershell
cd E:\Agent\backend
..\.venv\Scripts\python.exe tests\verify_db_monitoring.py
```

```
[1] PostgreSQL 直连采集
PASS  连接测试成功  连接成功 (62ms)
PASS  立即采集成功
      PostgreSQL 指标: 9 个指标
      [('db.connections.active', 1.0), ('db.connections.total', 6.0),
       ('db.connections.max', 100.0), ('db.transactions.committed', 966.0)]

[2] MySQL 直连采集
PASS  连接测试成功  连接成功 (46ms)
      MySQL / MariaDB 指标: 6 个指标
      [('db.connections.threads_connected', 1.0), ('db.connections.max_used', 1.0),
       ('db.queries.total', 24.0), ('db.slow_queries', 0.0),
       ('db.innodb.buffer_hit_rate', 94.6)]

[3] 错误密码应给出清晰错误      ✅
[4] 实例可编辑（此前只能删除）  ✅ PATCH 200，且留空密码不会清空凭据
[5] 数据库指标按实例正确归属    ✅ 序列名显示为实例名
```

定时采集链路也已验证：启用 PostgreSQL 脚本（间隔 20s）后，
数据库采样点从 30 增至 **48**（2 轮 × 9 指标）。

---

## L6：备份与容灾链路验证

```powershell
cd E:\Agent\backend
..\.venv\Scripts\python.exe tests\verify_backup.py
```

用一个「会执行备份命令」的 mock SSH 主机验证整条链路
（不在用户生产机上创建文件）：

| 验证项 | 结果 |
|---|---|
| 模板按分组可选（逻辑备份 / 配置备份） | ✅ 6 个模板 |
| 模板自带保留期清理 | ✅ 含 `find ... -mtime ... -delete` |
| 选中模板自动填充命令 | ✅ |
| 手动执行备份 | ✅ exit=0 |
| 识别备份文件路径 | ✅ |
| **命令行中不含密码** | ✅ 以 `PGPASSWORD='...'` 环境变量注入 |
| 统计被清理的过期文件数 | ✅ purged=2 |
| 执行历史（状态/退出码/耗时/产物） | ✅ |
| 任务上回写最近结果与关联实例名 | ✅ |
| 任务可修改（部分更新） | ✅ 此前 422 |
| 启用后注册调度 | ✅ |
| 任务可删除 | ✅ |

---

## L7：真实 Kingbase（人大金仓）验证

这是国产化环境的关键验证。目标机器为麒麟测试机 `192.168.203.128`。

### 环境

```
KingbaseES V009R001C010（开发版，365 天授权）
安装目录：/data/Kingbase/ES/V9/KESRealPro/V009R001C010
数据目录：/data/Kingbase/ES/V9/data
端口    ：54321（默认值，kingbase.conf 中 port 被注释）
监听    ：listen_addresses = '*'
认证    ：sys_hba 放行 0.0.0.0/0，scram-sha-256
系统    ：银河麒麟 Kylin V10 (Halberd)
```

### 排查到的四个问题

**① 服务根本没启动。**
只有 SSH(22) 在监听，没有 systemd 单元也没有 init 脚本，`kingbase.conf` 存在但
没有任何数据库进程。用 `sys_ctl` 手工拉起（`tests/start_kingbase.py`），
启动后 54321 立即监听。

**② 对外 54321 被防火墙拦截。**
即使 `listen_addresses='*'` 且 `sys_hba` 放行 `0.0.0.0/0`，
从本机直连 `192.168.203.128:54321` 仍然超时——**连服务器自己连自己的对外地址也超时**。
因此 direct 直连模式不可用，必须走 SSH 跳板；而且跳板内要连 `127.0.0.1`。
这直接催生了实例配置里的「从跳板机连接的地址」字段。

**③ `ksql` 不在 SSH 会话的 PATH 中。**
直接调用得到 `bash: ksql：未找到命令`（exit 127）。
催生了「客户端路径」字段，并在未指定时自动搜索常见安装位置，
找不到时给出明确提示而不是让用户猜。

**④ `ksql` 不认 `PGPASSWORD`——这是最隐蔽的一个。**
设置了 `PGPASSWORD` 它照样弹出交互式口令提示；而 SSH 命令的 stdin 一直开着，
于是命令**既不报错也不返回，一直挂到超时**。实测对比：

| 方式 | 结果 |
|---|---|
| `PGPASSWORD=... ksql ...` | ✗ 挂起直到超时 |
| `PGPASSWORD=... ksql ... < /dev/null` | ✗ `fe_sendauth: no password supplied` |
| `printf '%s\n' "$DB_PASSWORD" \| ksql -W ...` | ✅ 正常返回 |

最终采用管道方案：命令行里只出现 `$DB_PASSWORD` 变量名，
**明文口令不会出现在 `ps` 输出中**。

### 采集结果

```powershell
cd E:\Agent\backend
..\.venv\Scripts\python.exe tests\verify_kingbase_real.py
```

```
PASS  ★ Kingbase 连接成功  连接成功 (155ms)
PASS  ★ 采集成功
PASS  指标已落库  写入 8 个
      Kingbase 人大金仓指标: 8 个指标  78ms
         db.connections.active              = 1.0
         db.connections.total               = 8.0
         db.connections.max                 = 10.0
         db.transactions.committed          = 625.0
         db.transactions.rolled_back        = 10.0
         db.blocks.hit_rate                 = 98.5
         db.replication.lag_sec             = 0.0
         db.locks.waiting                   = 0.0
PASS  ★ 指标可按实例查询到  [('麒麟测试机 Kingbase', 8.0, 'database')]
```

定时采集同样验证通过：启用脚本（间隔 30s）后，
`db.connections.total` 按周期持续写入。

### 关于系统视图的一个更正

实测发现该版本 **`pg_*` 与 `sys_*` 前缀都可用**（`SELECT count(*) FROM
pg_stat_activity` 与 `sys_stat_activity` 均返回结果）。
但为了在更严格的版本上也能工作，内置脚本统一使用 `sys_*` 前缀。
`tests/test_kingbase.py` 中有断言防止把 PostgreSQL 的视图名混进金仓脚本。

回归测试 `tests/test_kingbase.py`（21 个用例）锁定了上述四个坑的行为。

### 防火墙放开后：直连模式也已验证

用户随后开放了服务器防火墙，直连模式随即可用：

```
本机 → 192.168.203.128:54321   TcpTestSucceeded = True
psycopg 直连                    成功，16ms，KingbaseES V009R001C010
实例 id=1（direct，库名留空）    连接成功 46ms
直连采集                        8 个指标，30ms
```

**顺带暴露了第 5 个问题（第 20 项缺陷）**：该实例没有填库名，
而我的代码把默认库名写成了 PostgreSQL 的 `postgres`，
在 Kingbase 上得到：

```
FATAL: 数据库 "postgres" 不存在
```

**Kingbase 的默认维护库叫 `kingbase`。** 现已按数据库类型区分默认库名：

| 类型 | 默认库 |
|---|---|
| postgresql | `postgres` |
| kingbase | `kingbase` |
| oracle | `ORCL` |

同时把「库不存在」这类错误改成可操作提示（实测输出）：

```
连接 Kingbase 人大金仓 192.168.203.128:54321 失败：目标库 "nosuchdb" 不存在。
该服务器上可用的库：kingbase、security、template0、template1、test。
请在实例配置中填写正确的「库名 / 服务名」。
```

服务器上实际存在的库：`kingbase`、`security`、`template0`、`template1`、`test`。

至此 **Kingbase 的两种采集模式（直连 / SSH 跳板）都在真实库上验证通过**。

---

## L8：大盘可见性与内置脚本同步

### 大盘可见性验证

用户反馈「指标采了但总览大屏看不到」。检查后确认大盘接口只统计主机，
**完全没有数据库字段**。修复后新增：

- `GET /dashboard/summary` 增加 `database_total` / `database_online` / `databases`
- 新增 `GET /databases/overview`（含归一化字段，供大盘与列表页复用）
- 前端大盘新增「数据库概况」区块与「数据库在线」统计卡

`frontend/tests/e2e_dashboard_db.mjs`（27 项）验证真实渲染，实测输出：

```
大盘统计卡：服务器在线 1/7 · 未恢复告警 0 · 严重告警 0 · 启用中脚本 3/12 · 数据库在线 4/4

数据库概况
实例                  类型              状态   连接数    命中率     复制延迟  锁等待
test                  Kingbase 人大金仓  在线   8/10 80%  99.54%    0s       0
Docker PostgreSQL     PostgreSQL        在线   6/100 6%  99.39%    0s       0
Docker MySQL          MySQL / MariaDB   在线   1/151 0.7% 94.69%   -        -
麒麟测试机 Kingbase     Kingbase 人大金仓  在线   8/10 80%  99.54%    0s       0
```

### 内置脚本同步机制

排查大盘时发现改进后的 MySQL 脚本（新增 `db.connections.max`）在已有安装上
**永远不生效**——原来的种子逻辑只在 `metric_script` 表为空时插入。

已改为：内置脚本的**内容由代码拥有**，每次启动按名称同步
（正文、解析规则、运行时、平台、db_type），同时**保留**用户可调的运行参数
（启用状态、采集周期、超时、作用域）。这与界面上的规则一致：内置脚本的正文本就禁止修改。

`tests/test_dashboard_db.py`（11 个用例）锁定了大盘字段、归一化逻辑与同步行为。

---

## L9：趋势预测验证

### 设计取舍

用**最小二乘线性拟合**而非预测库：短时间内指标基本线性，斜率就是「每天涨多少」，
**用户能自己校对**，且无额外依赖、结果可复现。

关键在于诚实：**一个不准的预测比没有预测更糟**，因此加了三道防线。

### 实测发现的假警报（第 32 项缺陷）

第一版对所有指标一律线性外推，连接数得出：

```
数据库连接数  当前=9.0  日均=+38.264  R²=0.71  可信=中  约 37 分钟后
```

R²=0.71 看起来相当可信，但结论毫无意义 —— 连接数在 `[0, 上限]` 之间来回跳，
不是单调增长量。这类数字只会造成告警疲劳。

修正：把指标分为两模式。

| 模式 | 指标 | 结论形式 |
|---|---|---|
| growth | 磁盘、inode、内存、交换分区、库容量、数据行数 | 线性外推 → 「约 N 天后达到 100%」 |
| peak | 数据库连接数、活跃连接数 | **不外推**，显示峰值距上限的余量 |

修正后同一指标的输出：

```
数据库连接数  当前=9.0  峰值=9  余量仅 10.0%  注: 窗口内峰值 9／上限 10
```

### 三道防误导规则

1. **低可信度不给剩余时间**：样本 <8 或 R²<0.4 时只显示增长率并标注
   「波动较大，剩余时间不可靠」。宁可不给数字。
2. **波动型不外推**：见上表。
3. **CPU 不纳入**：天然剧烈波动，拟合只产生噪声。

### 实测输出（真实数据，含持续写入负载）

```
对象                     指标        当前值     日均变化            预测              R²      可信
test(主机)               内存使用率   40.31%   +6.571%/天        约 9.1 天后       0.499   中
test(主机)               交换分区     0.46%    +0.242%/天        —（不可靠）       0.208   低
test(数据库)             数据行数     44000    +857382 行/天      —                1.0     高
麒麟测试机 Kingbase(库)   数据行数     44000    +857317 行/天      —                1.0     高
test(数据库)             数据库容量   22.78MB  +104.608MB/天      —                1.0     高
test(数据库)             数据库连接数  9        峰值 9             余量仅 10.0%      0.695   中
```

`tests/test_forecast.py`（25 个用例）锁定了拟合精度、两类模式、以及
「低可信度不得给出剩余时间」「波动型不得有线性 ETA」这两条硬约束。

---

## L10：真实数据库备份验证

之前的备份验证用的是 mock 主机，只证明了平台侧编排逻辑。这次**真的产出备份文件**。

### Kingbase（完整平台链路）

```
平台 → SSH 到麒麟服务器 → sys_dump → 真实 .dump 文件 → sys_restore 校验

PASS  ★ 备份执行成功  exit=0  407ms
      BACKUP_FILE=/var/backups/aiops/realtest/Kingbase_..._20261007_185127.dump
      -rw------- 1 root root 586K  ...
      ; Archive created at 2026-10-07 18:51:26 CST
      ;     dbname: test
      ARCHIVE_OK
PASS  ★ 归档可读（sys_restore -l 通过）
```

**586KB 归档，且 `sys_restore -l` 能列出内容** —— 这才叫备份可用，
而不是「文件生成了就算成功」。

### 这一轮暴露的四个真实缺陷

**① `sys_dump` 和 `ksql` 一样不认 `PGPASSWORD`**（第 26 项）

| 方式 | 结果 |
|---|---|
| `PGPASSWORD=... sys_dump ...` | ✗ 挂起直到超时 |
| `printf '%s\n' "$DB_PASSWORD" \| sys_dump -W ...` | ✅ 586KB |

与之前采集时踩的坑同源。金仓这套工具链似乎统一不读 PGPASSWORD。

**② SSH 执行不关闭 stdin**（第 27 项）

这是让 ① 表现为「600 秒超时」而不是立刻报错的原因。
`asyncssh` 的 `conn.run()` 默认不关闭 stdin，于是提示口令的命令会一直等输入。
修复：`conn.run(cmd, input=b"")` 显式关闭。

效果立竿见影 —— 同样的错误命令现在 **57ms 就返回**
`bash: sys_dump：未找到命令`，而不是让人干等 10 分钟。

**③ 密码注入用了前缀赋值**（第 29 项）

```bash
DB_PASSWORD='xxx' set -e        # 只对 set -e 这一条命令生效！
mkdir -p ...
sys_dump ...                    # 这里 $DB_PASSWORD 是空的
```

`VAR=x cmd` 的语义只影响**那一条命令**。多行脚本必须用 `export VAR=x;`。
（有意思的是：采集路径因为首条是 `CLIENT=$(...)` 纯赋值，恰好侥幸可用。）

**④ 换行符未归一化**（第 30 项）

在 Windows 上编辑过的脚本带 `\r\n`，Linux 的 `sh` 会把 `\r` 当成参数，
报出 `set: -: invalid option` 这种完全看不出原因的错。
已在 `render_script` 里统一转为 `\n`。

### MySQL

本机是 Windows，**没有可用的 POSIX shell**（无 Git Bash，WSL 未安装发行版），
而备份命令是 POSIX shell 语法，因此无法在本机通过 SSH 跑通完整平台链路。

改为用**平台渲染出的同一条命令**，在 MySQL 容器内（Linux `sh`）执行：

```
BACKUP_FILE=/tmp/aiops-bak/mysql_verify_20261007_185205.sql.gz
-rw-r--r-- 1 root root 587K
GZIP_OK
```

取出后校验内容：`CREATE TABLE` 4 条、`shop_` 相关 26 行、`INSERT` 6 条，
头部为 `-- MySQL dump 10.13 Distrib 8.4.11`。产物有效。

> 这一条同时说明了一个真实的部署要求：**备份的执行主机必须是 Linux（或具备 POSIX shell）**。
> 这不是缺陷，而是备份命令的固有前提，已写入「已知限制」。

### 测试数据

用户授权后，在两个库中构造了 `shop_users` / `shop_products` / `shop_orders`
（各 2000 / 500 / 20000 行），并启动持续写入（每 5 秒各插 50 条订单），
使事务数、库容量、行数持续增长 —— 趋势预测需要「动起来」的数据才有意义。
`tests/seed_test_data.py` 保留该脚本以便复现。

---

## 验证过程中发现并修复的真实缺陷

这些是**只有真实运行才会暴露**的问题，全部已修复：

| # | 缺陷 | 影响 | 发现方式 |
|---|---|---|---|
| 1 | `ChannelHandle` 是默认 dataclass（不可哈希） | `set.add()` 抛异常，**每次建立真实 SSH 终端都失败** | L2 真实 SSH 测试 |
| 2 | 误从 `SSHClientChannel` 读取输出 | 该对象无 `read()`，终端**收不到任何输出** | L2 真实 SSH 测试 |
| 3 | asyncssh 读写 API 用错（`proc.write`/`proc.read`） | 真实环境下无法写入与读取 | L2 真实 SSH 测试 |
| 4 | ORM 对象在 Session 关闭后被访问 | `DetachedInstanceError`，**WebSocket 建会话即崩** | L3 浏览器测试 + 服务端日志 |
| 5 | 前端本地回显排除了当前面板 | 输入的面板**一片空白**，另外三台才有字 | 代码审阅 |
| 6 | 模板模式 `{{host}}` 未实现 | UI 宣称的功能实际无效 | 代码审阅 |
| 7 | `runCommand` 未传 `operator` | 命令**不进审计日志** | L1 测试 |
| 8 | `convertEol: false` | 收到裸 `\n` 时输出**错位换行** | 截图目视检查 |
| 9 | JSON 解析器不支持逐行 JSONL | Python 脚本 `ctx.emit()` 产出**解析为空** | L1 测试 |
| 10 | `PATCH /scripts/{id}` 拒绝**一切**修改 | **内置脚本永远无法启用**（默认停用），且提示指向不存在的「复制」功能 | 用户反馈 |
| 11 | `runtime == "sql"` 落到 shell 分支 | SQL 被当作 shell 命令通过 SSH 执行，**数据库监控完全没有采集** | 用户反馈 + 代码核查 |
| 12 | 数据库实例只有 GET/POST/DELETE | **添加后只能删除，改不了** | 用户反馈 |
| 13 | 更新接口复用「创建用」schema | 部分更新返回 422；**数据库实例与备份任务各犯一次** | L2c 回归测试 |
| 14 | `has_secret` 从未赋值 | 界面永远显示「未保存」，用户以为密码没存进去 | L2c 回归测试 |
| 15 | 外键引用未校验 | 传入不存在的服务器 ID 直接 **500 IntegrityError** | L2c 回归测试 |
| 16 | `PRAGMA temp_store` 未设置 | SQLite 重建表时因临时目录不可写报 `SQLITE_CANTOPEN`（**报错信息"无法打开数据库文件"极具误导性**），**平台无法启动** | 迁移失败排查 |
| 17 | 跳板命令假设客户端在 PATH 中 | 报 `bash: ksql: 未找到命令`（exit 127），用户不知该填什么 | L7 真实 Kingbase |
| 18 | 金仓口令用 `PGPASSWORD` 传递 | ksql 不认该变量 → 转交互式提示 → **命令挂起到超时**（既不报错也不返回） | L7 真实 Kingbase |
| 19 | 跳板模式使用对外地址连接 | 数据库只监听本机/内网时**连不上**；催生「跳板内连接地址」字段 | L7 真实 Kingbase |
| 20 | 金仓默认库名用了 `postgres` | 库名留空时报 `FATAL: 数据库 "postgres" 不存在`；**金仓的默认维护库叫 `kingbase`** | L7 真实 Kingbase |
| 21 | 缺 `GET /databases/{id}` 单实例接口 | 查详情返回 404 | L7 排查时发现 |
| 22 | 「库不存在」错误不可操作 | 用户不知道该填哪个库名，只能反复试 | L7 真实 Kingbase |
| 23 | 大盘完全没有数据库信息 | 指标**采到了却看不见**，用户「完全不清楚数据库的情况」 | 用户反馈 |
| 24 | 内置脚本内容不随升级更新 | 种子逻辑只在空表时执行；改进脚本后**老库永远跑旧版本**（新增指标采不到） | 大盘排查时发现 |
| 25 | MySQL 未采集 `max_connections` | 算不出连接数水位，大盘该列空白 | 大盘排查时发现 |
| 26 | 金仓备份模板用 `PGPASSWORD` | **sys_dump 与 ksql 一样不认该变量** → 转交互式提示 → 备份**挂起 600 秒后超时**（实测确认） | L10 真实备份 |
| 27 | `SSHExecutor.run` 不关闭 stdin | 任何会提示输入的命令都表现为「既不报错也不返回」直到超时 | L10 真实备份 |
| 28 | 备份模板裸调 `sys_dump` | `bash: sys_dump：未找到命令`（exit 127），用户不知道该填什么 | L10 真实备份 |
| 29 | 密码注入用了 `VAR=x 脚本` 前缀 | 前缀只对**第一条**命令生效，多行脚本里变量为空 → `fe_sendauth: no password supplied` | L10 真实备份 |
| 30 | 脚本未归一化换行符 | 用户从 Windows 粘贴的 `\r\n` 脚本会让 shell 报 `set: -: invalid option` | L10 真实备份 |
| 31 | 预测查询用全局 `LIMIT` 升序 | 主机指标每天 3 万+ 采样点，**最新样本被截掉** → 新指标「有数据却预测不出来」 | L9 趋势预测 |
| 32 | 连接数被当作增长型外推 | 得出「37 分钟后连接数耗尽」的**假警报**（R²=0.71 看似可信） | L9 趋势预测 |

> 第 1~3 项属于典型的「单元测试用假实现就测不出来」的问题——
> 假执行器绕过了真实的 asyncssh API，只有接入真实 SSH 才暴露。
>
> 第 10~12 项是**用户反馈**发现的，都指向同一类错误：
> 我写了保护逻辑，却没验证它是否把正常操作也一起挡住了。

### 一个隐蔽的 SQLite 坑（第 16 项）

重建 SQLite 表（把 `server_id` 改为可空）时报
`unable to open database file`（SQLITE_CANTOPEN）。逐语句定位后发现失败点是
`DROP TABLE`，而**真正原因是 SQLite 在 WAL 模式下需要一个临时文件，
而临时目录不在工作区内、被沙箱拒绝**。

`PRAGMA temp_store=MEMORY` 可彻底规避。由于任何大结果集的排序/GROUP BY
都可能触发同样问题，该设置已加到**所有连接**上，而不只是迁移。

另外，SQLAlchemy 的 `engine.begin()` 配合 pysqlite 时 DDL 会被**隐式提交**，
导致"建新表已提交、拷数据的 INSERT 回滚"，留下空的半成品表。
因此重建表必须用**原生 sqlite3 + 显式事务**，不能走 SQLAlchemy 事务。

### 测试基础设施的一个坑

mock SSH 服务器最初在收到窗口尺寸变更时会断开连接。
原因：**asyncssh 以「在 `stdin.read()` 中抛出 `TerminalSizeChanged` 异常」
的方式通知尺寸变化**，处理协程必须捕获该异常并继续读取，否则协程终止即连接关闭。
这不是平台缺陷，但会让端到端测试出现「连接建立后立刻断开」的假象。
已在两个 mock 中修复。

另外：开启 VPN 后，`httpx` 会读取 Windows 注册表里的系统代理，
把本机 `127.0.0.1` 的请求也送到代理，表现为莫名其妙的 **502**。
直连本机服务的测试客户端必须设置 `trust_env=False`。

---

## 环境说明

开发初期本机出现**所有子进程均以 `0xC0000142` 退出**的故障
（连 `Write-Output` 都失败）。经排查确认为 **DSH 受限沙箱模式**所致，
切换到完全权限后恢复正常。诊断过程记录在
[ENVIRONMENT-ISSUE.md](ENVIRONMENT-ISSUE.md)。

Docker Hub 直连不通（IPv6 超时），改用可达的镜像源
`docker.m.daocloud.io` 拉取验证用的数据库镜像。

---

## 未覆盖的部分

诚实说明以下内容**尚未经过运行验证**：

1. **Windows Server（WinRM）执行器** —— 未实现
2. **Oracle 的真实连接采集** —— 驱动与 SQL 脚本已就绪，但本机没有 Oracle，
   未做真实连接测试（MySQL / PostgreSQL / Kingbase 均已在真实库上验证）
3. **备份执行主机必须是 Linux** —— 备份命令是 POSIX shell 语法，
   本机是 Windows 且无可用的 POSIX shell，因此 MySQL 的**平台链路**未在本机跑通
   （改用容器内执行同一条渲染命令来验证模板本身）。
   生产环境的数据库主机本身就是 Linux，这属于固有的部署前提，不是缺陷。
4. **备份的恢复演练** —— 已验证归档**可读**（`sys_restore -l` / `gzip -t` / 内容抽样），
   但未做完整的「恢复到另一个库」演练
5. **大文件分片上传与断点续传** —— 未实现
6. **大规模并发（50+ 台）** —— 只在 4 台规模验证
7. **真实 Linux 服务器的 `stty -echo` 行为** —— 用 mock 服务器验证；
   mock 以「回显已关闭」启动，平台是否**下发** `stty -echo` 已单独断言，
   但真实 shell 收到该命令后的行为未在真实 Linux 上验证
8. **备份过程中密码在 argv 中短暂可见** —— 密码以 `export VAR=...` 形式随
   `sh -c` 传入，在该 shell 进程的 `ps` 输出中可见（同主机其他用户可读）。
   彻底规避需改用连接 stdin 传递，当前未实现

---

## 附：真实环境的连接故障定位
用户反馈「MobaXterm 能连、平台连不上，且不显示报错」。定位过程与结论：

**现象**：数据库中该服务器的 `last_error` 为 `连接失败：`（冒号后**空白**）。

**根因一：错误信息为空。**
Python 3.11+ 中 `asyncio.TimeoutError` 就是内建 `TimeoutError`，
它是 `OSError` 的子类且 `str()` 为空字符串。原代码
`f"连接失败：{exc}"` 因此产出空白提示。

**根因二：前端吞掉了错误。**
`server.status` 处理器只保留状态、丢弃消息；随后到达的 `session.ready`
又用空消息覆盖了一次。

**排除项**（逐项实测确认）：

| 假设 | 实测结果 |
|---|---|
| 网络不通 | ✗ 排除：TCP 与 banner 均正常 |
| 算法不兼容（老旧设备） | ✗ 排除：`SSH-2.0-OpenSSH_8.2`，协商成功 |
| 密码错误 | ✗ 排除：用已保存的密码诊断，认证成功 |
| 凭据解密链路损坏 | ✗ 排除：`verify_credential_path.py` 全通过 |

**实际原因**：该错误是一次**超时**（空消息即 TimeoutError 的特征），
而当时平台实例运行在受限的执行环境中，对局域网的外连被阻断；
MobaXterm 作为用户桌面的普通进程不受此限制，因此能连通。
在正常权限下重新运行平台后，连接立即成功（61ms）。

**修复**：为每类异常映射可操作的中文说明（超时带秒数、拒绝、重置、
认证失败、密钥不兼容、连接中断），保证消息永不为空；
前端在面板上直接显示失败原因；新增分阶段诊断接口与界面。
回归测试见 `tests/test_error_messages.py` 与 `frontend/tests/e2e_conn_error.mjs`。

---
