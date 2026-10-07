"""数据库指标在大盘可见的回归测试，以及内置脚本同步机制。

背景（用户反馈）：
  「你监控了数据库的各项指标，但是我在总览大屏都没看到相关数据，
   我完全不清楚数据库的情况」
——指标采集了却不在总览页呈现，等于没采。这里锁定修复后的行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402


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
def instance(client):
    r = client.post(
        "/api/v1/databases",
        json={
            "name": "大盘测试库",
            "db_type": "kingbase",
            "host": "10.5.5.5",
            "port": 54321,
            "username": "system",
            "secret": "x",
            "database_name": "test",
        },
    )
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    yield r.json()
    client.delete(f"/api/v1/databases/{iid}")


# ============================================================ 大盘字段


def test_summary_exposes_database_counts(client):
    """★ 大盘汇总必须包含数据库数量，否则总览页看不到数据库。"""
    s = client.get("/api/v1/dashboard/summary").json()
    assert "database_total" in s, "大盘缺少 database_total"
    assert "database_online" in s, "大盘缺少 database_online"
    assert "databases" in s, "大盘缺少 databases 明细"
    assert isinstance(s["databases"], list)


def test_summary_database_counts_are_consistent(client, instance):
    s = client.get("/api/v1/dashboard/summary").json()
    assert s["database_total"] == len(s["databases"])
    assert s["database_online"] <= s["database_total"]


def test_database_overview_endpoint(client, instance):
    o = client.get("/api/v1/databases/overview").json()
    assert o["total"] >= 1
    assert "online" in o
    assert isinstance(o["databases"], list)
    names = [d["name"] for d in o["databases"]]
    assert "大盘测试库" in names


def test_overview_item_has_normalized_fields(client, instance):
    """★ 归一化字段让前端不必关心各数据库指标名不同。"""
    o = client.get("/api/v1/databases/overview").json()
    item = next(d for d in o["databases"] if d["name"] == "大盘测试库")
    for field in (
        "id", "name", "db_type", "status", "last_error", "collect_mode",
        "connections", "connections_max", "connection_usage_pct",
        "hit_rate", "lag_sec", "locks_waiting", "metrics", "metric_count",
        "updated_at",
    ):
        assert field in item, f"概况缺少字段 {field}"


def test_overview_includes_all_db_types(client):
    """概况应覆盖所有实例（不因类型不同而遗漏）。"""
    instances = client.get("/api/v1/databases").json()
    overview = client.get("/api/v1/databases/overview").json()["databases"]
    assert len(overview) == len(instances), "概况遗漏了部分实例"


def test_single_instance_endpoint_exists(client, instance):
    """★ 曾经缺少 GET /databases/{id}，查详情返回 404。"""
    r = client.get(f"/api/v1/databases/{instance['id']}")
    assert r.status_code == 200, f"单实例接口不可用：{r.status_code}"
    assert r.json()["name"] == "大盘测试库"
    assert r.json()["has_secret"] is True


# ============================================================ 归一化逻辑


def test_overview_normalizes_connection_metric_names():
    """不同数据库的连接数指标名不同，应归一化为 connections。"""
    from app.services.alert_engine import compute_database_overview

    # 只验证候选名列表被正确覆盖（真实取值需要采样数据）
    import inspect

    src = inspect.getsource(compute_database_overview)
    for candidate in (
        "db.connections.total",
        "db.connections.threads_connected",
        "db.connections.active",
    ):
        assert candidate in src, f"未覆盖连接数候选指标 {candidate}"
    for hit in ("db.blocks.hit_rate", "db.innodb.buffer_hit_rate"):
        assert hit in src, f"未覆盖命中率候选指标 {hit}"


def test_overview_handles_no_instances(client):
    """没有实例时应返回空列表而不是报错。"""
    from app.db import SessionLocal
    from app.models import DatabaseInstance
    from app.services.alert_engine import compute_database_overview

    db = SessionLocal()
    try:
        saved = db.query(DatabaseInstance).all()
        for inst in saved:
            db.delete(inst)
        db.commit()
        try:
            assert compute_database_overview(db) == []
        finally:
            # 复原
            for inst in saved:
                db.add(
                    DatabaseInstance(
                        id=inst.id, name=inst.name, db_type=inst.db_type,
                        host=inst.host, port=inst.port, username=inst.username,
                        database_name=inst.database_name,
                        collect_mode=inst.collect_mode,
                    )
                )
            db.commit()
    finally:
        db.close()


# ============================================================ 内置脚本同步


def test_builtin_scripts_synced_from_code(client):
    """★ 内置脚本内容由代码拥有，启动时应同步到最新版本。

    否则升级后老库一直跑旧脚本——例如新增了一个指标，
    采集不到却看不出原因。
    """
    from app.builtin_scripts import BUILTIN_SCRIPTS
    from app.db import SessionLocal
    from app.models import MetricScript

    db = SessionLocal()
    try:
        scripts = {
            s.name: s
            for s in db.query(MetricScript).filter(MetricScript.is_builtin.is_(True)).all()
        }
        for spec in BUILTIN_SCRIPTS:
            cur = scripts.get(spec["name"])
            assert cur is not None, f"内置脚本缺失：{spec['name']}"
            assert cur.body == spec["body"], f"{spec['name']} 正文未同步到最新"
            assert cur.emit_schema == spec["emit_schema"], f"{spec['name']} 解析规则未同步"
            assert cur.db_type == spec.get("db_type", ""), f"{spec['name']} db_type 不一致"
    finally:
        db.close()


def test_mysql_script_collects_max_connections():
    """★ MySQL 脚本必须采集连接上限，否则算不出连接数水位。"""
    from app.builtin_scripts import MYSQL_BODY

    assert "db.connections.max" in MYSQL_BODY
    assert "@@max_connections" in MYSQL_BODY


def test_sync_preserves_user_runtime_settings(client):
    """同步内容时不能覆盖用户设置的启用状态与周期。"""
    from app.db import SessionLocal
    from app.main import seed_defaults
    from app.models import MetricScript

    db = SessionLocal()
    try:
        script = (
            db.query(MetricScript)
            .filter(MetricScript.is_builtin.is_(True))
            .first()
        )
        script.enabled = True
        script.interval_sec = 123
        db.add(script)
        db.commit()
        sid, name = script.id, script.name
    finally:
        db.close()

    # 再次执行种子逻辑（模拟重启）
    seed_defaults()

    db = SessionLocal()
    try:
        again = db.get(MetricScript, sid)
        assert again.name == name
        assert again.enabled is True, "同步把用户的启用状态覆盖了"
        assert again.interval_sec == 123, "同步把用户的采集周期覆盖了"
    finally:
        db.close()
