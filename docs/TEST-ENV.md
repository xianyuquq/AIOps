# 测试环境说明（本地 Docker 数据库）

本文件记录本地测试用容器数据库的**完整连接方式**。

> 为什么单独写这个文件：这两个容器是验证数据库监控与备份容灾用的，
> 但连接信息此前只散落在几个测试脚本的常量里，没有集中的说明，
> 导致想用 DBeaver 或 PowerShell 手工连一下时找不到口令。

---

## 一、连接信息总表

| | PostgreSQL | MySQL |
|---|---|---|
| **容器名** | `aiops-pg` | `aiops-mysql` |
| **镜像** | `docker.m.daocloud.io/library/postgres:16-alpine` | `docker.m.daocloud.io/library/mysql:8` |
| **主机** | `127.0.0.1` | `127.0.0.1` |
| **端口** | `5432` | `3306` |
| **用户** | `postgres` | `root` |
| **密码** | `Test1234` | `Test1234` |
| **数据库** | `testdb` | `testdb` |
| **JDBC URL** | `jdbc:postgresql://127.0.0.1:5432/testdb` | `jdbc:mysql://127.0.0.1:3306/testdb` |

> ⚠️ 这是**本地一次性测试容器**的固定口令，明文写在仓库里是刻意为之，
> 目的是让任何人都能连上去排查。**绝不要用在任何真实环境。**

### 镜像名为什么带前缀

最初 `docker pull postgres:16-alpine` 直连 Docker Hub 会因 IPv6 超时失败，
因此改用了 `docker.m.daocloud.io` 镜像源，本地镜像名就带上了这个前缀。

**`docker run` 时必须用带前缀的完整名字**，否则会报 `Unable to find image locally`
（本地没有 `postgres:16-alpine` 这个 tag）。

### 表结构

两个库都有一致的三张业务表，用于让监控指标（库大小、行数、事务数）真实变化：

| 表 | 行数 |
|---|---|
| `shop_users` | 2,000 |
| `shop_products` | 500 |
| `shop_orders` | 20,000 |

---

## 二、用 PowerShell 连接

PowerShell 原生不带 PostgreSQL / MySQL 驱动。三种可用方式：

### 方式 A：借助容器内的客户端（无需装任何东西，推荐）

```powershell
# PostgreSQL
docker exec -e PGPASSWORD=Test1234 aiops-pg psql -h 127.0.0.1 -U postgres -d testdb -c "\dt"
docker exec -e PGPASSWORD=Test1234 aiops-pg psql -h 127.0.0.1 -U postgres -d testdb `
  -c "SELECT count(*) FROM shop_orders;"

# MySQL
docker exec -e MYSQL_PWD=Test1234 aiops-mysql mysql -h 127.0.0.1 -uroot -D testdb -e "SHOW TABLES;"
docker exec -e MYSQL_PWD=Test1234 aiops-mysql mysql -h 127.0.0.1 -uroot -D testdb `
  -e "SELECT count(*) FROM shop_orders;"
```

### 方式 B：用项目虚拟环境里的 Python 驱动

项目已装好 `psycopg` 与 `PyMySQL`，适合写查询脚本：

```powershell
cd E:\Agent\backend
..\.venv\Scripts\python.exe -c "import psycopg; c=psycopg.connect(host='127.0.0.1',port=5432,user='postgres',password='Test1234',dbname='testdb'); cur=c.cursor(); cur.execute('SELECT count(*) FROM shop_orders'); print(cur.fetchone())"
```

### 方式 C：通过平台自身的接口

平台已经把连接测试和采集做成了接口，可以直接看到"平台眼里这两个库是什么样"：

```powershell
$body = @{username='admin';password='admin123'} | ConvertTo-Json
$tok  = (Invoke-RestMethod "http://127.0.0.1:8848/api/v1/auth/login/json" `
         -Method Post -Body $body -ContentType "application/json").access_token
$h    = @{Authorization = "Bearer $tok"}

# 实例 2 = PostgreSQL，3 = MySQL
Invoke-RestMethod "http://127.0.0.1:8848/api/v1/databases/2/test" -Method Post -Headers $h
Invoke-RestMethod "http://127.0.0.1:8848/api/v1/databases/2/collect" -Method Post -Headers $h
```

---

## 三、用 DBeaver 连接

本机 DBeaver 的位置（免安装版，非 Program Files）：

```
E:\DBeaver\dbeaver-ce-26.2.2-windows-x86_64\dbeaver\dbeaver.exe
```

连接参数（同上表）：

| 字段 | PostgreSQL | MySQL |
|---|---|---|
| 驱动 | PostgreSQL | MySQL |
| Host | `127.0.0.1` | `127.0.0.1` |
| Port | `5432` | `3306` |
| Database | `testdb` | `testdb` |
| Username | `postgres` | `root` |
| Password | `Test1234` | `Test1234` |

### 连不上时，先查这一条：驱动下载

DBeaver 首次连接某个数据库时会去下载 JDBC 驱动。**本机驱动缓存是空的**
（`%APPDATA%\DBeaverData\drivers` 下 0 个文件），所以第一次连接必然要联网下载。

而本机直连外网不通，**必须走系统代理 `127.0.0.1:7892`**。
DBeaver 默认不读系统代理，因此会卡在驱动下载上，表现为"连接失败"，
很容易误以为是口令错了。

**解决办法（任选其一，都不需要安装任何东西）：**

**办法 A：给 DBeaver 配代理**

`窗口(Window) → 首选项(Preferences) → 通用(General) → 网络连接(Network Connections)`
→ Active Provider 选 `Manual` → 在 HTTP / HTTPS 两行都填：

```
Host: 127.0.0.1    Port: 7892
```

确定后**重新建连接**（或右键已有连接 → 编辑连接 → 测试连接），
DBeaver 会重新触发驱动下载，这次能成功。

若菜单里没有该项，也可在 `首选项 → 连接 → 驱动 → Maven` 里找代理设置。

**办法 B：手工放驱动 jar**

用浏览器（浏览器走系统代理，是通的）下载对应 jar：

- PostgreSQL：`https://jdbc.postgresql.org/download/postgresql-42.7.4.jar`
- MySQL：`https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/9.1.0/mysql-connector-j-9.1.0.jar`

然后 `数据库 → 驱动管理器 → 选中 PostgreSQL/MySQL → 编辑 → 库 → 添加文件`，
指向下载的 jar。之后建连接就不再联网。

### 其他常见卡点

- PostgreSQL 报 `The server does not support SSL`：
  `连接设置 → SSL` 里取消勾选 `Use SSL`。
- 端口连不上：**先确认容器真的在跑**（见下），
  容器处于 `Exited` / `Created` 时端口不会监听。

> ```powershell
> docker ps -a --filter "name=aiops-"
> ```

---

## 四、容器的创建与管理（用 compose）

这两个容器由 **docker compose 项目 `aiops-test-db`** 管理，编排文件在仓库里：

```
E:\Agent\deploy\test-db\docker-compose.yml
```

### 为什么从裸 docker run 改成 compose

早先用裸 `docker run` 建，有三个后果：

| 问题 | 后果 |
|---|---|
| 没有 `restart` 策略 | **开机 / Docker 重启后容器不会自己起来**，在只显示运行中容器的列表里就像"消失了" |
| 没有命名数据卷 | 容器一旦被删，**数据全丢** |
| 不在 compose 项目下 | 容器是散着的，不好统一启停 |

现在三点都解决了：

```
aiops-pg      RestartPolicy=unless-stopped   Mounts=aiops-pg-data
aiops-mysql   RestartPolicy=unless-stopped   Mounts=aiops-mysql-data
```

- **开机自启**：`unless-stopped` —— 除非你手动停过，否则 Docker 启动后自动拉起
- **数据持久化**：数据在命名卷 `aiops-pg-data` / `aiops-mysql-data` 里，
  即使 `docker compose down` 再 `up`，数据依然在
- **Docker Desktop 里可直接启停**：项目名 `aiops-test-db`，两个容器归在一起

> 已实测：容器重启前后 `shop_orders` 均为 20000 行。

### 常用命令

```powershell
cd E:\Agent\deploy\test-db

docker compose up -d        # 启动（首次会创建容器与数据卷）
docker compose ps           # 看状态
docker compose stop         # 停止（保留容器与数据）
docker compose start        # 再启动
docker compose down         # 停止并移除容器，数据卷保留
docker compose down -v      # ⚠️ 连数据卷一起删，数据全丢
docker compose logs -f      # 看日志
```

也可以直接在 **Docker Desktop 的 Containers 页面**点按钮启停。

### 灌入测试数据

```powershell
cd E:\Agent\backend
$env:AIOPS_TEST_KINGBASE_PASSWORD = "你的金仓口令"   # 金仓部分需要；连不上会自动跳过
..\.venv\Scripts\python.exe tests\seed_test_data.py --once
```

> `seed_test_data.py` 目前只覆盖 **Kingbase 与 MySQL**。
> PostgreSQL 用专门的脚本（同样是幂等的，重复运行不会重复插入）：
>
> ```powershell
> cd E:\Agent\backend
> ..\.venv\Scripts\python.exe tests\seed_postgres.py
> ```
>
> 输出示例：
> ```
> PostgreSQL testdb: shop_users=2,000  shop_products=500  shop_orders=20,000
> ```

### 若容器彻底不见了

```powershell
cd E:\Agent\deploy\test-db
docker compose up -d
```

只要数据卷还在，数据就会回来：

```powershell
docker volume ls --filter "name=aiops"
```

> `aiops-mysql` 有时初次 `docker run -d` 会停在 `Created` 状态不自动启动，
> 补一条 `docker start aiops-mysql` 即可。

---

## 五、关于删除操作的一条纪律

本文件之所以存在，是因为一次事故：**在没有确认容器状态的情况下执行了
`docker rm -f aiops-pg aiops-mysql`，把两个测试库删掉了。**

当时的错误链条：
1. `docker ps`（只看运行中的）看不到容器，误判为"不存在"
   —— 实际是 Docker Desktop 没运行、容器处于停止状态
2. `docker rm -f ... 2>&1 | Out-Null` 把输出丢弃，事后无法追溯
3. 删除与重建写在一起，重建又因**镜像名写错**（漏了镜像源前缀）而失败

因此约定：

- **删除前必须 `docker ps -a` 确认目标确实存在及其状态**
- **不要用 `Out-Null` 吞掉删除命令的输出**
- **删除与重建要在同一次操作里确认成功**，不要"先删了再说"
- 生产环境同理：`docker rm -f` / `DROP` / `rm -rf` 之前先看一眼
