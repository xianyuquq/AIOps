"""监控脚本管理的回归测试。

背景：曾经有一个真实缺陷——后端的 PATCH 接口拒绝**所有**对内置脚本的修改，
包括"启用/停用"。而内置脚本默认是停用的，于是用户永远无法启用它们，
界面上点「启用」只会得到 400。

这里的用例锁定正确的行为边界：
  - 允许改「运行参数」：启用/停用、采集周期、超时、作用域
  - 不允许改「采集内容」：脚本正文、解析规则、运行时、平台、名称、分类
  - 必须能「复制」出可自由编辑的副本
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
        resp = c.post(
            "/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


def _builtin_id(client, name_part: str = "Linux") -> int:
    scripts = client.get("/api/v1/scripts").json()
    for s in scripts:
        if s["is_builtin"] and name_part in s["name"]:
            return s["id"]
    raise AssertionError(f"未找到内置脚本：{name_part}")


def _get(client, script_id: int) -> dict:
    return client.get(f"/api/v1/scripts/{script_id}").json()


# ============================================================ 启用/停用


def test_builtin_scripts_start_disabled(client):
    """内置脚本默认停用（等用户配置好服务器后再开启）。"""
    scripts = client.get("/api/v1/scripts").json()
    builtins = [s for s in scripts if s["is_builtin"]]
    assert builtins, "应存在内置脚本"
    assert any(not s["enabled"] for s in builtins), "内置脚本默认应为停用状态"


def test_enable_builtin_script_via_minimal_payload(client):
    """★ 核心回归：只发 enabled 字段即可启用内置脚本。"""
    sid = _builtin_id(client)
    resp = client.patch(f"/api/v1/scripts/{sid}", json={"enabled": True})
    assert resp.status_code == 200, f"启用失败：{resp.status_code} {resp.text}"
    assert resp.json()["enabled"] is True
    assert _get(client, sid)["enabled"] is True


def test_disable_builtin_script(client):
    sid = _builtin_id(client)
    resp = client.patch(f"/api/v1/scripts/{sid}", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_toggle_with_full_payload_echo_is_accepted(client):
    """前端曾把整个对象回传，字段值未变时不应被拒绝。

    这是原缺陷的直接触发路径：值相同却被当成「修改了采集内容」。
    """
    scripts = client.get("/api/v1/scripts").json()
    src = next(s for s in scripts if s["is_builtin"])

    payload = {
        "name": src["name"],
        "category": src["category"],
        "runtime": src["runtime"],
        "platform": src["platform"],
        "body": src["body"],
        "emit_schema": src["emit_schema"],
        "interval_sec": src["interval_sec"],
        "timeout_sec": src["timeout_sec"],
        "target_scope": src["target_scope"],
        "enabled": True,
    }
    resp = client.patch(f"/api/v1/scripts/{src['id']}", json=payload)
    assert resp.status_code == 200, (
        f"原样回传全部字段被误判为修改采集内容：{resp.status_code} {resp.text}"
    )
    assert resp.json()["enabled"] is True


# ============================================================ 运行参数可改


def test_can_change_interval_and_timeout(client):
    sid = _builtin_id(client)
    resp = client.patch(
        f"/api/v1/scripts/{sid}", json={"interval_sec": 45, "timeout_sec": 25}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["interval_sec"] == 45
    assert data["timeout_sec"] == 25


def test_can_change_target_scope(client):
    sid = _builtin_id(client)
    resp = client.patch(
        f"/api/v1/scripts/{sid}", json={"target_scope": {"servers": [1, 2]}}
    )
    assert resp.status_code == 200
    assert resp.json()["target_scope"] == {"servers": [1, 2]}


# ============================================================ 采集内容不可改


@pytest.mark.parametrize(
    "field,value",
    [
        ("body", "echo hacked 1"),
        ("name", "改名试试"),
        ("runtime", "python"),
        ("platform", "windows"),
        ("category", "database"),
    ],
)
def test_cannot_change_builtin_content(client, field, value):
    """内置脚本的采集内容必须受保护，并给出可操作的提示。"""
    sid = _builtin_id(client)
    resp = client.patch(f"/api/v1/scripts/{sid}", json={field: value})
    assert resp.status_code == 400, f"字段 {field} 本应被拒绝：{resp.status_code}"
    detail = resp.json().get("detail", "")
    assert "内置脚本" in detail, f"错误提示应说明原因：{detail}"
    assert "复制" in detail, f"错误提示应告诉用户如何定制：{detail}"


def test_cannot_change_emit_schema(client):
    sid = _builtin_id(client)
    resp = client.patch(
        f"/api/v1/scripts/{sid}", json={"emit_schema": {"format": "json", "metrics": []}}
    )
    assert resp.status_code == 400


# ============================================================ 复制与自定义


def test_duplicate_creates_editable_copy(client):
    """★ 复制出的副本必须可自由编辑内容。"""
    sid = _builtin_id(client)
    source = _get(client, sid)

    resp = client.post(f"/api/v1/scripts/{sid}/duplicate")
    assert resp.status_code == 200, resp.text
    copy = resp.json()

    try:
        assert copy["is_builtin"] is False, "副本不应被标记为内置"
        assert copy["enabled"] is False, "副本默认停用，避免立刻重复采集"
        assert copy["body"] == source["body"], "副本应继承脚本正文"
        assert copy["emit_schema"] == source["emit_schema"]
        assert copy["id"] != source["id"]
        assert copy["name"] != source["name"]

        # 副本内容可以改
        resp = client.patch(
            f"/api/v1/scripts/{copy['id']}", json={"body": "echo my.metric 1"}
        )
        assert resp.status_code == 200, f"副本应可编辑：{resp.text}"
        assert resp.json()["body"] == "echo my.metric 1"

        # 副本可以启用
        resp = client.patch(f"/api/v1/scripts/{copy['id']}", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
    finally:
        client.delete(f"/api/v1/scripts/{copy['id']}")


def test_duplicate_names_do_not_collide(client):
    sid = _builtin_id(client)
    first = client.post(f"/api/v1/scripts/{sid}/duplicate").json()
    second = client.post(f"/api/v1/scripts/{sid}/duplicate").json()
    try:
        assert first["name"] != second["name"], "多次复制不应重名"
    finally:
        client.delete(f"/api/v1/scripts/{first['id']}")
        client.delete(f"/api/v1/scripts/{second['id']}")


def test_cannot_delete_builtin_script(client):
    sid = _builtin_id(client)
    resp = client.delete(f"/api/v1/scripts/{sid}")
    assert resp.status_code == 400


# ============================================================ 采集任务注册


def test_enabling_registers_collection_job(client):
    """启用脚本后必须真的注册采集任务，否则「启用」只是改了个标记。"""
    from app.services.collector import scheduler

    sid = _builtin_id(client)
    client.patch(f"/api/v1/scripts/{sid}", json={"enabled": True})

    job_id = f"metric_script_{sid}"
    assert scheduler.get_job(job_id) is not None, (
        f"启用后未注册采集任务 {job_id}；已注册："
        f"{[j.id for j in scheduler.get_jobs()]}"
    )

    client.patch(f"/api/v1/scripts/{sid}", json={"enabled": False})
    assert scheduler.get_job(job_id) is None, "停用后应移除采集任务"


def test_collector_reload_endpoint(client):
    resp = client.post("/api/v1/collector/reload")
    assert resp.status_code == 200
    data = resp.json()
    assert "scripts_registered" in data
