"""内置监控脚本。

输出约定：每行 `metric_key value [label=value ...]`
这种格式的好处是：任何 shell 命令都能通过一行 awk/echo 接入，
用户不需要学习新 DSL。
"""

from __future__ import annotations

# --------------------------------------------------------------------- Linux 系统指标

LINUX_SYSTEM_BODY = r"""
# CPU 使用率（基于 /proc/stat 差值需要状态，这里用瞬时快照近似）
read -r _ u n s i w irq sirq st _ < /proc/stat
total=$((u+n+s+i+w+irq+sirq+st))
idle=$((i+w))
if [ "$total" -gt 0 ]; then
  cpu=$(awk "BEGIN{printf \"%.2f\", (1 - $idle/$total) * 100}")
  echo "cpu.usage_percent $cpu"
fi

# 负载
read -r l1 l5 l15 _ < /proc/loadavg
echo "load.1m $l1"
echo "load.5m $l5"
echo "load.15m $l15"

# 内存
mem_total=$(awk '/MemTotal/{print $2}' /proc/meminfo)
mem_avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
if [ -n "$mem_total" ] && [ "$mem_total" -gt 0 ]; then
  echo "mem.total_mb $(awk "BEGIN{printf \"%.0f\", $mem_total/1024}")"
  echo "mem.used_percent $(awk "BEGIN{printf \"%.2f\", (1 - $mem_avail/$mem_total) * 100}")"
fi

# 交换分区
swap_total=$(awk '/SwapTotal/{print $2}' /proc/meminfo)
swap_free=$(awk '/SwapFree/{print $2}' /proc/meminfo)
if [ -n "$swap_total" ] && [ "$swap_total" -gt 0 ]; then
  echo "swap.used_percent $(awk "BEGIN{printf \"%.2f\", (1 - $swap_free/$swap_total) * 100}")"
fi

# 磁盘（df -PTh 的输出列数因系统而异，-P 保证 POSIX 格式）
df -PT 2>/dev/null | awk 'NR>1 && $2 !~ /^(tmpfs|devtmpfs|overlay|squashfs|iso9660)$/ {
  gsub(/%/,"",$6);
  print "disk.used_percent " $6 " mount=" $7
}'

# 磁盘容量（MB）
df -PT 2>/dev/null | awk 'NR>1 && $2 !~ /^(tmpfs|devtmpfs|overlay|squashfs|iso9660)$/ {
  print "disk.total_mb " int($3/1024) " mount=" $7
}'

# inode 使用率
df -Pi 2>/dev/null | awk 'NR>1 && $2 !~ /^(tmpfs|devtmpfs|overlay|squashfs|iso9660)$/ {
  gsub(/%/,"",$5);
  print "disk.inode_used_percent " $5 " mount=" $6
}'

# 网络流量（累计字节，平台侧做速率换算）
awk -F'[: ]+' 'NR>2 { if ($2 != "lo") { print "net.rx_bytes " $3 " iface=" $2; print "net.tx_bytes " $11 " iface=" $2 } }' /proc/net/dev

# 磁盘 IO（累计扇区数 × 512 = 字节）
awk '$3 ~ /^(sd|vd|nvme|hd)/ { print "disk.io_read_bytes " $6*512 " dev=" $3; print "disk.io_write_bytes " $10*512 " dev=" $3 }' /proc/diskstats

# 进程数与文件句柄
echo "proc.count $(ps -e --no-headers 2>/dev/null | wc -l)"
if [ -r /proc/sys/fs/file-nr ]; then
  read -r alloc _ max < /proc/sys/fs/file-nr
  echo "fs.file_handles_used $alloc"
  echo "fs.file_handles_max $max"
fi

# TCP 连接状态
if command -v ss >/dev/null 2>&1; then
  echo "tcp.established $(ss -Htn state established 2>/dev/null | wc -l)"
  echo "tcp.time_wait $(ss -Htn state time-wait 2>/dev/null | wc -l)"
  echo "tcp.listen $(ss -Htln 2>/dev/null | wc -l)"
fi

# 运行时长
awk '{printf "system.uptime_sec %.0f\n", $1}' /proc/uptime
"""

# --------------------------------------------------------------------- Windows

WINDOWS_SYSTEM_BODY = r"""
# CPU 使用率
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
Write-Output "cpu.usage_percent $cpu"

# 内存
$os = Get-CimInstance Win32_OperatingSystem
$totalMB = [math]::Round($os.TotalVisibleMemorySize / 1024, 0)
$freeMB  = [math]::Round($os.FreePhysicalMemory / 1024, 0)
$usedPct = [math]::Round((1 - $freeMB / $totalMB) * 100, 2)
Write-Output "mem.total_mb $totalMB"
Write-Output "mem.used_percent $usedPct"

# 磁盘
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    $pct = [math]::Round((1 - $_.FreeSpace / $_.Size) * 100, 2)
    $totalMb = [math]::Round($_.Size / 1MB, 0)
    Write-Output "disk.used_percent $pct mount=$($_.DeviceID)"
    Write-Output "disk.total_mb $totalMb mount=$($_.DeviceID)"
}

# 进程数
Write-Output "proc.count $((Get-Process).Count)"

# 运行时长
$uptime = ((Get-Date) - $os.LastBootUpTime).TotalSeconds
Write-Output "system.uptime_sec $([math]::Round($uptime, 0))"

# 网络（累计字节）
Get-CimInstance Win32_PerfRawData_Tcpip_NetworkInterface | ForEach-Object {
    Write-Output "net.rx_bytes $($_.BytesReceivedPersec) iface=$($_.Name -replace ' ','_')"
    Write-Output "net.tx_bytes $($_.BytesSentPersec) iface=$($_.Name -replace ' ','_')"
}
"""

# --------------------------------------------------------------------- 数据库（SQL）

MYSQL_BODY = r"""
SELECT 'db.connections.threads_connected', VARIABLE_VALUE + 0
FROM performance_schema.global_status WHERE VARIABLE_NAME='Threads_connected'
UNION ALL SELECT 'db.connections.max_used', VARIABLE_VALUE + 0
FROM performance_schema.global_status WHERE VARIABLE_NAME='Max_used_connections'
-- 连接上限取自系统变量（不在 global_status 里），用于计算连接数水位
UNION ALL SELECT 'db.connections.max', @@max_connections + 0
UNION ALL SELECT 'db.queries.total', VARIABLE_VALUE + 0
FROM performance_schema.global_status WHERE VARIABLE_NAME='Queries'
UNION ALL SELECT 'db.slow_queries', VARIABLE_VALUE + 0
FROM performance_schema.global_status WHERE VARIABLE_NAME='Slow_queries'
UNION ALL SELECT 'db.innodb.buffer_hit_rate',
  ROUND(100 - (SELECT VARIABLE_VALUE FROM performance_schema.global_status
               WHERE VARIABLE_NAME='Innodb_buffer_pool_reads') * 100 /
        GREATEST((SELECT VARIABLE_VALUE FROM performance_schema.global_status
               WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests'), 1), 2)
UNION ALL SELECT 'db.threads.running', COUNT(*) FROM information_schema.processlist
WHERE COMMAND <> 'Sleep'
-- 库容量：随时间增长，是趋势预测（还有多久需要扩容）的核心依据
UNION ALL SELECT 'db.size_mb',
  ROUND(SUM(COALESCE(data_length,0) + COALESCE(index_length,0)) / 1024 / 1024, 2)
  FROM information_schema.tables WHERE table_schema = DATABASE()
UNION ALL SELECT 'db.rows.total', COALESCE(SUM(table_rows), 0)
  FROM information_schema.tables WHERE table_schema = DATABASE();
"""

POSTGRESQL_BODY = r"""
SELECT 'db.connections.active', count(*) FROM pg_stat_activity WHERE state = 'active'
UNION ALL
SELECT 'db.connections.total', count(*) FROM pg_stat_activity
UNION ALL
SELECT 'db.connections.max', setting::numeric FROM pg_settings WHERE name='max_connections'
UNION ALL
SELECT 'db.transactions.committed', sum(xact_commit) FROM pg_stat_database
UNION ALL
SELECT 'db.transactions.rolled_back', sum(xact_rollback) FROM pg_stat_database
UNION ALL
SELECT 'db.blocks.hit_rate',
  ROUND(sum(blks_hit) * 100.0 / GREATEST(sum(blks_hit) + sum(blks_read), 1), 2)
  FROM pg_stat_database
UNION ALL
SELECT 'db.replication.lag_sec',
  COALESCE(EXTRACT(EPOCH FROM max(now() - pg_last_xact_replay_timestamp())), 0)
UNION ALL
SELECT 'db.locks.waiting', count(*) FROM pg_locks WHERE NOT granted
UNION ALL
SELECT 'db.deadlocks', sum(deadlocks) FROM pg_stat_database
-- 库容量：趋势预测（还有多久需要扩容）的核心依据
UNION ALL
SELECT 'db.size_mb', ROUND(pg_database_size(current_database()) / 1024.0 / 1024.0, 2)
UNION ALL
SELECT 'db.rows.total', COALESCE(sum(n_live_tup), 0) FROM pg_stat_user_tables;
"""

# Kingbase 兼容 PG 协议，但系统视图前缀是 sys_ 而非 pg_，列名亦有差异。
# 不能直接复用 PostgreSQL 脚本——这是国产化环境实施中最容易踩的坑。
KINGBASE_BODY = r"""
SELECT 'db.connections.active', count(*) FROM sys_stat_activity WHERE state = 'active'
UNION ALL
SELECT 'db.connections.total', count(*) FROM sys_stat_activity
UNION ALL
SELECT 'db.connections.max', setting::numeric FROM sys_settings WHERE name='max_connections'
UNION ALL
SELECT 'db.transactions.committed', sum(xact_commit) FROM sys_stat_database
UNION ALL
SELECT 'db.transactions.rolled_back', sum(xact_rollback) FROM sys_stat_database
UNION ALL
SELECT 'db.blocks.hit_rate',
  ROUND(sum(blks_hit) * 100.0 / GREATEST(sum(blks_hit) + sum(blks_read), 1), 2)
  FROM sys_stat_database
UNION ALL
SELECT 'db.replication.lag_sec',
  COALESCE(EXTRACT(EPOCH FROM max(now() - sys_last_xact_replay_timestamp())), 0)
UNION ALL
SELECT 'db.locks.waiting', count(*) FROM sys_locks WHERE NOT granted
-- 库容量：趋势预测（还有多久需要扩容）的核心依据
UNION ALL
SELECT 'db.size_mb', ROUND(sys_database_size(current_database()) / 1024.0 / 1024.0, 2)
UNION ALL
SELECT 'db.rows.total', COALESCE(sum(n_live_tup), 0) FROM sys_stat_user_tables;
"""

ORACLE_BODY = r"""
SELECT 'db.connections.active' AS metric_key, COUNT(*) AS value
  FROM v$session WHERE status = 'ACTIVE'
UNION ALL
SELECT 'db.connections.total', COUNT(*) FROM v$session
UNION ALL
SELECT 'db.connections.max', TO_NUMBER(value) FROM v$parameter WHERE name = 'sessions'
UNION ALL
SELECT 'db.sessions.blocked', COUNT(*) FROM v$session WHERE blocking_session IS NOT NULL
UNION ALL
SELECT 'db.library_cache.hit_rate',
       ROUND(gethitratio * 100, 2) FROM v$librarycache
 WHERE namespace = 'SQL AREA' AND ROWNUM = 1
UNION ALL
SELECT 'db.sga.free_mb',
       ROUND(value / 1024 / 1024, 0) FROM v$parameter WHERE name = 'sga_target'
UNION ALL
SELECT 'db.tablespace.used_percent',
       ROUND(MAX(used_percent), 2) FROM dba_tablespace_usage_metrics
FROM dual;
"""

# --------------------------------------------------------------------- 注册表

BUILTIN_SCRIPTS = [
    {
        "name": "Linux 系统指标",
        "category": "system",
        "runtime": "shell",
        "platform": "linux",
        "body": LINUX_SYSTEM_BODY.strip(),
        "interval_sec": 60,
        "timeout_sec": 20,
        "emit_schema": {
            "format": "line_kv",
            "metrics": [
                {"key": "cpu.usage_percent", "unit": "%", "type": "gauge"},
                {"key": "mem.used_percent", "unit": "%", "type": "gauge"},
                {"key": "mem.total_mb", "unit": "MB", "type": "gauge"},
                {"key": "load.1m", "unit": "", "type": "gauge"},
                {"key": "load.5m", "unit": "", "type": "gauge"},
                {"key": "load.15m", "unit": "", "type": "gauge"},
                {"key": "disk.used_percent", "unit": "%", "type": "gauge", "labels": ["mount"]},
                {"key": "disk.total_mb", "unit": "MB", "type": "gauge", "labels": ["mount"]},
                {"key": "disk.inode_used_percent", "unit": "%", "type": "gauge", "labels": ["mount"]},
                {"key": "net.rx_bytes", "unit": "bytes", "type": "counter", "labels": ["iface"]},
                {"key": "net.tx_bytes", "unit": "bytes", "type": "counter", "labels": ["iface"]},
                {"key": "disk.io_read_bytes", "unit": "bytes", "type": "counter", "labels": ["dev"]},
                {"key": "disk.io_write_bytes", "unit": "bytes", "type": "counter", "labels": ["dev"]},
                {"key": "proc.count", "unit": "count", "type": "gauge"},
                {"key": "tcp.established", "unit": "count", "type": "gauge"},
                {"key": "tcp.time_wait", "unit": "count", "type": "gauge"},
                {"key": "tcp.listen", "unit": "count", "type": "gauge"},
                {"key": "fs.file_handles_used", "unit": "count", "type": "gauge"},
                {"key": "system.uptime_sec", "unit": "s", "type": "counter"},
                {"key": "swap.used_percent", "unit": "%", "type": "gauge"},
            ],
        },
    },
    {
        "name": "Windows 系统指标",
        "category": "system",
        "runtime": "shell",
        "platform": "windows",
        "body": WINDOWS_SYSTEM_BODY.strip(),
        "interval_sec": 60,
        "timeout_sec": 30,
        "emit_schema": {
            "format": "line_kv",
            "metrics": [
                {"key": "cpu.usage_percent", "unit": "%", "type": "gauge"},
                {"key": "mem.used_percent", "unit": "%", "type": "gauge"},
                {"key": "mem.total_mb", "unit": "MB", "type": "gauge"},
                {"key": "disk.used_percent", "unit": "%", "type": "gauge", "labels": ["mount"]},
                {"key": "disk.total_mb", "unit": "MB", "type": "gauge", "labels": ["mount"]},
                {"key": "proc.count", "unit": "count", "type": "gauge"},
                {"key": "system.uptime_sec", "unit": "s", "type": "counter"},
            ],
        },
    },
    {
        "name": "MySQL / MariaDB 指标",
        "category": "database",
        "runtime": "sql",
        "db_type": "mysql",
        "platform": "any",
        "body": MYSQL_BODY.strip(),
        "interval_sec": 60,
        "timeout_sec": 15,
        "emit_schema": {
            "format": "line_kv",
            "metrics": [
                {"key": "db.connections.threads_connected", "unit": "count", "type": "gauge"},
                {"key": "db.connections.max_used", "unit": "count", "type": "gauge"},
                {"key": "db.queries.total", "unit": "count", "type": "counter"},
                {"key": "db.slow_queries", "unit": "count", "type": "counter"},
                {"key": "db.innodb.buffer_hit_rate", "unit": "%", "type": "gauge"},
                {"key": "db.threads.running", "unit": "count", "type": "gauge"},
            ],
        },
    },
    {
        "name": "PostgreSQL 指标",
        "category": "database",
        "runtime": "sql",
        "db_type": "postgresql",
        "platform": "any",
        "body": POSTGRESQL_BODY.strip(),
        "interval_sec": 60,
        "timeout_sec": 15,
        "emit_schema": {
            "format": "line_kv",
            "metrics": [
                {"key": "db.connections.active", "unit": "count", "type": "gauge"},
                {"key": "db.connections.total", "unit": "count", "type": "gauge"},
                {"key": "db.connections.max", "unit": "count", "type": "gauge"},
                {"key": "db.transactions.committed", "unit": "count", "type": "counter"},
                {"key": "db.transactions.rolled_back", "unit": "count", "type": "counter"},
                {"key": "db.blocks.hit_rate", "unit": "%", "type": "gauge"},
                {"key": "db.replication.lag_sec", "unit": "s", "type": "gauge"},
                {"key": "db.locks.waiting", "unit": "count", "type": "gauge"},
                {"key": "db.deadlocks", "unit": "count", "type": "counter"},
            ],
        },
    },
    {
        "name": "Kingbase 人大金仓指标",
        "category": "database",
        "runtime": "sql",
        "db_type": "kingbase",
        "platform": "any",
        "body": KINGBASE_BODY.strip(),
        "interval_sec": 60,
        "timeout_sec": 15,
        "emit_schema": {
            "format": "line_kv",
            "metrics": [
                {"key": "db.connections.active", "unit": "count", "type": "gauge"},
                {"key": "db.connections.total", "unit": "count", "type": "gauge"},
                {"key": "db.connections.max", "unit": "count", "type": "gauge"},
                {"key": "db.transactions.committed", "unit": "count", "type": "counter"},
                {"key": "db.transactions.rolled_back", "unit": "count", "type": "counter"},
                {"key": "db.blocks.hit_rate", "unit": "%", "type": "gauge"},
                {"key": "db.replication.lag_sec", "unit": "s", "type": "gauge"},
                {"key": "db.locks.waiting", "unit": "count", "type": "gauge"},
            ],
        },
    },
    {
        "name": "Oracle 指标",
        "category": "database",
        "runtime": "sql",
        "db_type": "oracle",
        "platform": "any",
        "body": ORACLE_BODY.strip(),
        "interval_sec": 60,
        "timeout_sec": 15,
        "emit_schema": {
            "format": "line_kv",
            "metrics": [
                {"key": "db.connections.active", "unit": "count", "type": "gauge"},
                {"key": "db.connections.total", "unit": "count", "type": "gauge"},
                {"key": "db.connections.max", "unit": "count", "type": "gauge"},
                {"key": "db.sessions.blocked", "unit": "count", "type": "gauge"},
                {"key": "db.library_cache.hit_rate", "unit": "%", "type": "gauge"},
                {"key": "db.sga.free_mb", "unit": "MB", "type": "gauge"},
                {"key": "db.tablespace.used_percent", "unit": "%", "type": "gauge"},
            ],
        },
    },
]

BUILTIN_ALERT_RULES = [
    {"name": "CPU 使用率过高", "metric_key": "cpu.usage_percent", "operator": ">",
     "threshold": 90, "duration_sec": 300, "severity": "warning"},
    {"name": "CPU 使用率严重过高", "metric_key": "cpu.usage_percent", "operator": ">",
     "threshold": 95, "duration_sec": 120, "severity": "critical"},
    {"name": "内存使用率过高", "metric_key": "mem.used_percent", "operator": ">",
     "threshold": 90, "duration_sec": 300, "severity": "warning"},
    {"name": "磁盘即将写满", "metric_key": "disk.used_percent", "operator": ">",
     "threshold": 85, "duration_sec": 300, "severity": "warning"},
    {"name": "磁盘即将写满（严重）", "metric_key": "disk.used_percent", "operator": ">",
     "threshold": 95, "duration_sec": 60, "severity": "critical"},
    {"name": "inode 即将耗尽", "metric_key": "disk.inode_used_percent", "operator": ">",
     "threshold": 90, "duration_sec": 300, "severity": "warning"},
    {"name": "系统负载过高", "metric_key": "load.5m", "operator": ">",
     "threshold": 8, "duration_sec": 300, "severity": "warning"},
    {"name": "数据库连接数接近上限", "metric_key": "db.connections.total", "operator": ">",
     "threshold": 180, "duration_sec": 180, "severity": "warning"},
    {"name": "数据库锁等待", "metric_key": "db.locks.waiting", "operator": ">",
     "threshold": 5, "duration_sec": 120, "severity": "warning"},
    {"name": "主从复制延迟", "metric_key": "db.replication.lag_sec", "operator": ">",
     "threshold": 30, "duration_sec": 180, "severity": "warning"},
    {"name": "交换分区使用率过高", "metric_key": "swap.used_percent", "operator": ">",
     "threshold": 50, "duration_sec": 600, "severity": "warning"},
]
