"""数据库实例与备份任务的回归测试。

这里锁定的是一类我**犯了两次**的错误：
更新接口复用了「创建用」的 Pydantic schema，而创建 schema 有必填字段，
于是只传一个字段的 PATCH 会返回 422 —— 前端表现为「只能删除，改不了」。

因此本文件重点断言：**部分更新必须成功，且未提供的字段保持不变**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

PG = {
    "name": "测试用 PostgreSQL",
    "db_type": "postgresql",
    "host": "10.1.1.10",
    "port": 5432,
    "username": "postgres",
    "secret": "secret-123",
    "database_name": "testdb",
    "collect_mode": "direct",
    "enabled": True,
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        assert r.status_code == 200, r.text
        c.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
        yield c


@pytest.fixture(scope="module")
def server_id(client):
    """备份任务需要引用一个「执行服务器」，这里建一台测试用的。"""
    r = client.post(
        "/api/v1/servers",
        json={
            "name": "测试执行主机",
            "host": "10.9.9.9",
            "port": 22,
            "username": "root",
            "protocol": "ssh",
            "auth_type": "password",
            "secret": "x",
        },
    )
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    yield sid
    client.delete(f"/api/v1/servers/{sid}")


@pytest.fixture
def instance(client):
    """每个用例一个干净的实例。"""
    r = client.post("/api/v1/databases", json=PG)
    assert r.status_code == 200, r.text
    data = r.json()
    yield data
    client.delete(f"/api/v1/databases/{data['id']}")


# ============================================================ 数据库实例


def test_create_instance_hides_secret(client, instance):
    assert instance["has_secret"] is True, "创建后应报告已保存凭据"
    assert "secret" not in instance
    assert PG["secret"] not in str(instance), "接口不得回传明文密码"


def test_list_reports_has_secret(client, instance):
    items = client.get("/api/v1/databases").json()
    mine = next(x for x in items if x["id"] == instance["id"])
    assert mine["has_secret"] is True, "列表也必须正确反映凭据状态"


def test_partial_update_name_only(client, instance):
    """★ 核心回归：只传 name 的 PATCH 必须成功（此前 422）。"""
    r = client.patch(f"/api/v1/databases/{instance['id']}", json={"name": "改名后"})
    assert r.status_code == 200, f"部分更新被拒绝：{r.status_code} {r.text}"
    assert r.json()["name"] == "改名后"


def test_partial_update_keeps_password(client, instance):
    """只改名称不应清空密码。"""
    r = client.patch(f"/api/v1/databases/{instance['id']}", json={"name": "改个名"})
    assert r.status_code == 200
    assert r.json()["has_secret"] is True, "编辑后密码被清空了"


def test_update_other_fields(client, instance):
    r = client.patch(
        f"/api/v1/databases/{instance['id']}",
        json={"host": "10.2.2.20", "port": 5433, "database_name": "other"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["host"] == "10.2.2.20"
    assert data["port"] == 5433
    assert data["database_name"] == "other"
    assert data["username"] == PG["username"], "未提供的字段应保持不变"


def test_update_password(client, instance):
    r = client.patch(f"/api/v1/databases/{instance['id']}", json={"secret": "new-pass"})
    assert r.status_code == 200
    assert r.json()["has_secret"] is True


def test_switch_collect_mode(client, instance, server_id):
    r = client.patch(
        f"/api/v1/databases/{instance['id']}",
        json={"collect_mode": "ssh_proxy", "proxy_server_id": server_id},
    )
    assert r.status_code == 200
    assert r.json()["collect_mode"] == "ssh_proxy"


def test_db_types_endpoint(client):
    data = client.get("/api/v1/databases/templates").json()
    keys = [x["key"] for x in data["db_types"]]
    assert set(keys) >= {"mysql", "postgresql", "oracle", "kingbase"}
    assert all(x["default_port"] > 0 for x in data["db_types"])


def test_delete_instance(client):
    r = client.post("/api/v1/databases", json={**PG, "name": "待删除实例"})
    iid = r.json()["id"]
    assert client.delete(f"/api/v1/databases/{iid}").status_code == 200
    assert all(x["id"] != iid for x in client.get("/api/v1/databases").json())


# ============================================================ 备份任务


@pytest.fixture
def backup_task(client, instance, server_id):
    tpl = client.get("/api/v1/backups/templates").json()["templates"]
    pg_tpl = next(t for t in tpl if t["key"] == "postgresql_logical")
    r = client.post(
        "/api/v1/backups",
        json={
            "name": "测试备份任务",
            "db_instance_id": instance["id"],
            "exec_server_id": server_id,
            "backup_type": "logical",
            "template_key": "postgresql_logical",
            "script": pg_tpl["script"],
            "target_dir": "/tmp/aiops-backup-test",
            "schedule_cron": "0 2 * * *",
            "retention_days": 7,
            "enabled": False,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    yield data
    client.delete(f"/api/v1/backups/{data['id']}")


def test_backup_templates_available(client):
    tpl = client.get("/api/v1/backups/templates").json()["templates"]
    keys = {t["key"] for t in tpl}
    assert {"mysql_logical", "postgresql_logical", "kingbase_logical",
            "oracle_logical"} <= keys, f"缺少备份模板：{keys}"
    # 每个模板都要带保留期清理，否则磁盘会被备份撑满
    for t in tpl:
        if t["backup_type"] == "logical":
            assert "mtime" in t["script"], f"{t['key']} 模板缺少过期清理"


def test_backup_task_created_disabled(client, backup_task):
    assert backup_task["enabled"] is False, "新建任务应默认停用"


def test_partial_update_backup_task(client, backup_task):
    """★ 核心回归：只传 retention_days 的 PATCH 必须成功（此前 422）。"""
    r = client.patch(
        f"/api/v1/backups/{backup_task['id']}", json={"retention_days": 3}
    )
    assert r.status_code == 200, f"部分更新被拒绝：{r.status_code} {r.text}"
    assert r.json()["retention_days"] == 3


def test_update_backup_schedule_and_enable(client, backup_task):
    r = client.patch(
        f"/api/v1/backups/{backup_task['id']}",
        json={"schedule_cron": "30 1 * * *", "enabled": True},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert r.json()["schedule_cron"] == "30 1 * * *"


def test_backup_task_reports_relation_names(client, backup_task, instance):
    items = client.get("/api/v1/backups").json()
    mine = next(t for t in items if t["id"] == backup_task["id"])
    assert mine["db_instance_name"] == instance["name"]


def test_backup_history_empty_at_first(client, backup_task):
    data = client.get(f"/api/v1/backups/{backup_task['id']}/runs").json()
    assert data["runs"] == []


def test_delete_backup_task(client, backup_task):
    assert client.delete(f"/api/v1/backups/{backup_task['id']}").status_code == 200
    items = client.get("/api/v1/backups").json()
    assert all(t["id"] != backup_task["id"] for t in items)


def test_deleting_instance_removes_its_backup_tasks(client, server_id):
    """删除数据库实例时应一并清理它的备份任务，避免悬空引用。"""
    inst = client.post("/api/v1/databases", json={**PG, "name": "级联删除测试"}).json()
    tpl = client.get("/api/v1/backups/templates").json()["templates"]
    pg_tpl = next(t for t in tpl if t["key"] == "postgresql_logical")
    task = client.post(
        "/api/v1/backups",
        json={
            "name": "级联任务",
            "db_instance_id": inst["id"],
            "exec_server_id": server_id,
            "script": pg_tpl["script"],
            "target_dir": "/tmp/x",
        },
    ).json()

    client.delete(f"/api/v1/databases/{inst['id']}")
    items = client.get("/api/v1/backups").json()
    assert all(t["id"] != task["id"] for t in items), "实例删除后备份任务仍残留"


def test_backup_command_rejects_dangerous_redirect(client, backup_task):
    """备份命令不得包含对块设备的写操作。"""
    from app.services.backup import _FORBIDDEN

    assert _FORBIDDEN.search("dd if=/dev/zero of=/dev/sda bs=1M")
    assert _FORBIDDEN.search("mkfs.ext4 /dev/sdb1")
    assert not _FORBIDDEN.search("pg_dump -h x -d y -Fc -f /var/backups/a.dump")


def test_backup_password_not_in_rendered_command(client, instance, backup_task):
    """★ 渲染后的命令中绝不能出现数据库密码。"""
    from app.db import SessionLocal
    from app.models import BackupTask, DatabaseInstance
    from app.services.backup import render_script

    db = SessionLocal()
    try:
        task = db.get(BackupTask, backup_task["id"])
        inst = db.get(DatabaseInstance, instance["id"])
        rendered = render_script(task, inst)
        assert PG["secret"] not in rendered, "密码被写进了备份命令"
        assert "{{" not in rendered, f"仍存在未替换的占位符：{rendered[:200]}"
        assert "pg_dump" in rendered
        # 命令里不应出现内联的密码赋值（注释里提到 PGPASSWORD 是允许的）
        assert "PGPASSWORD=" not in rendered, "密码被内联写进了命令"
        assert "MYSQL_PWD=" not in rendered, "密码被内联写进了命令"
    finally:
        db.close()


def test_password_env_uses_variable_not_inline(client, instance):
    """密码必须以环境变量形式返回，交由执行层注入。"""
    from app.db import SessionLocal
    from app.models import DatabaseInstance
    from app.services.backup import _password_env

    db = SessionLocal()
    try:
        inst = db.get(DatabaseInstance, instance["id"])
        env = _password_env(inst)
        assert env.get("PGPASSWORD") == PG["secret"]
    finally:
        db.close()
