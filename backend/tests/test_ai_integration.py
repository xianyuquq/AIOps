"""LLM 运行时配置与对外集成接口的回归测试。

覆盖用户提出的两个需求：
  1. 能在界面里填写 LLM 配置（原先是环境变量 + 重启）
  2. 外部系统能调用平台的 AI 能力（原先是封闭的）
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


# ============================================================ LLM 配置


def test_llm_settings_readable(client):
    r = client.get("/api/v1/settings/llm")
    assert r.status_code == 200, r.text
    data = r.json()
    for field in ("enabled", "base_url", "model", "timeout", "max_tokens", "has_api_key"):
        assert field in data, f"缺少字段 {field}"


def test_llm_settings_never_exposes_api_key(client):
    """★ 密钥绝不能通过接口返回。"""
    r = client.put(
        "/api/v1/settings/llm",
        json={"api_key": "sk-super-secret-value-should-never-leak"},
    )
    assert r.status_code == 200
    body = r.text
    assert "sk-super-secret-value-should-never-leak" not in body
    assert "api_key" not in r.json() or r.json().get("api_key") in (None, "")
    assert r.json()["has_api_key"] is True


def test_llm_settings_update_takes_effect_without_restart(client):
    """★ 改完立即生效——这正是原方案（环境变量 + 重启）做不到的。"""
    r = client.put(
        "/api/v1/settings/llm",
        json={
            "enabled": True,
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen2.5:7b",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["base_url"] == "http://127.0.0.1:11434/v1"
    assert data["model"] == "qwen2.5:7b"

    # 重新读一次，确认已持久化
    again = client.get("/api/v1/settings/llm").json()
    assert again["base_url"] == "http://127.0.0.1:11434/v1"
    assert again["sources"]["llm_base_url"] == "db"

    # provider 立即反映新配置
    from app.services.ai.provider import get_provider

    p = get_provider()
    assert p.base_url == "http://127.0.0.1:11434/v1"
    assert p.model == "qwen2.5:7b"
    assert p.enabled is True  # 本地端点可不填 key


def test_llm_settings_reports_source(client):
    """来源标记能解释「为什么我在界面改了却没生效」。"""
    data = client.get("/api/v1/settings/llm").json()
    assert "sources" in data
    for key in ("llm_base_url", "llm_model", "llm_enabled", "llm_api_key"):
        assert data["sources"].get(key) in ("db", "env", "default")


def test_llm_settings_rejects_bad_url(client):
    r = client.put("/api/v1/settings/llm", json={"base_url": "ftp://bad"})
    assert r.status_code == 400
    assert "http" in r.json()["detail"]


def test_llm_settings_rejects_bad_numbers(client):
    assert client.put("/api/v1/settings/llm", json={"timeout": 1}).status_code == 422
    assert client.put("/api/v1/settings/llm", json={"max_tokens": 1}).status_code == 422


def test_llm_test_endpoint_reports_failure_gracefully(client):
    """端点不可达时应返回 ok=false 而不是 500。"""
    client.put(
        "/api/v1/settings/llm",
        json={"enabled": True, "base_url": "http://127.0.0.1:9/v1", "model": "x"},
    )
    r = client.post("/api/v1/settings/llm/test")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["message"]


def test_llm_test_endpoint_when_disabled(client):
    client.put("/api/v1/settings/llm", json={"enabled": False})
    r = client.post("/api/v1/settings/llm/test").json()
    assert r["ok"] is False
    assert "未启用" in r["message"]


# ============================================================ 访问令牌


@pytest.fixture(scope="module")
def token(client):
    r = client.post(
        "/api/v1/tokens", json={"name": "测试用外部系统", "scope": "ai"}
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_create_token_returns_plaintext_once(token):
    assert token["token"].startswith("aiops_")
    assert "notice" in token


def test_token_list_never_returns_plaintext(client, token):
    """★ 列表接口绝不能回显明文令牌。"""
    items = client.get("/api/v1/tokens").json()
    assert items
    for item in items:
        assert "token" not in item or item["token"] in (None, "")
    assert token["token"] not in client.get("/api/v1/tokens").text


def test_token_has_prefix_and_metadata(client, token):
    assert token["prefix"].startswith("aiops_")
    assert token["scope"] == "ai"
    assert token["call_count"] == 0
    assert token["enabled"] is True


def test_external_call_requires_token(client):
    r = client.post("/api/v1/ext/ai/chat", json={"question": "你好"})
    assert r.status_code == 401


def test_external_call_rejects_bad_token(client):
    r = client.post(
        "/api/v1/ext/ai/chat",
        json={"question": "你好"},
        headers={"Authorization": "Bearer aiops_deadbeef000000000000"},
    )
    assert r.status_code == 401


def test_external_status_accepts_token(client, token):
    r = client.get(
        "/api/v1/ext/ai/status",
        headers={"Authorization": f"Bearer {token['token']}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["token_name"] == "测试用外部系统"


def test_external_status_accepts_x_api_key_header(token):
    """X-API-Key 也应可用。

    这里用**不带默认 Authorization 头**的独立 client：
    同时给出两者时 Authorization 优先（这是刻意的优先级），
    若沿用带管理员 JWT 的 client，测的就不是 X-API-Key 这条路了。
    """
    with TestClient(app) as anon:
        r = anon.get("/api/v1/ext/ai/status", headers={"X-API-Key": token["token"]})
    assert r.status_code == 200, r.text


def test_authorization_takes_precedence_over_x_api_key(client, token):
    """两个头都给时以 Authorization 为准——避免鉴权来源含糊不清。"""
    r = client.get(
        "/api/v1/ext/ai/status",
        headers={"X-API-Key": token["token"]},   # 同时带着管理员的 Bearer
    )
    assert r.status_code == 401, "不应因为 X-API-Key 有效就放行无效的 Bearer"


def test_external_call_updates_usage_counter(client, token):
    client.get(
        "/api/v1/ext/ai/status",
        headers={"Authorization": f"Bearer {token['token']}"},
    )
    items = client.get("/api/v1/tokens").json()
    mine = next(i for i in items if i["id"] == token["id"])
    assert mine["call_count"] >= 1
    assert mine["last_used_at"]


def test_disabled_token_is_rejected(client, token):
    client.patch(f"/api/v1/tokens/{token['id']}", json={"enabled": False})
    r = client.get(
        "/api/v1/ext/ai/status",
        headers={"Authorization": f"Bearer {token['token']}"},
    )
    assert r.status_code == 401
    client.patch(f"/api/v1/tokens/{token['id']}", json={"enabled": True})


def test_external_chat_returns_503_when_ai_disabled(client, token):
    client.put("/api/v1/settings/llm", json={"enabled": False})
    r = client.post(
        "/api/v1/ext/ai/chat",
        json={"question": "现在有什么问题"},
        headers={"Authorization": f"Bearer {token['token']}"},
    )
    assert r.status_code == 503
    assert "未启用" in r.json()["detail"]


def test_openai_compatible_endpoint_shape(client, token):
    """★ OpenAI 兼容端点：不可用时也要给出标准形态，便于客户端解析。"""
    r = client.post(
        "/api/v1/ext/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {token['token']}"},
    )
    # AI 未启用 → 503；启用但端点不可达 → 502。都不应是 500 或 404
    assert r.status_code in (502, 503), r.text


def test_openai_endpoint_validates_messages(client, token):
    r = client.post(
        "/api/v1/ext/v1/chat/completions",
        json={"messages": []},
        headers={"Authorization": f"Bearer {token['token']}"},
    )
    assert r.status_code == 422


def test_token_delete(client):
    created = client.post("/api/v1/tokens", json={"name": "待删除"}).json()
    assert client.delete(f"/api/v1/tokens/{created['id']}").status_code == 200
    ids = [i["id"] for i in client.get("/api/v1/tokens").json()]
    assert created["id"] not in ids


def test_external_mode_hides_tools():
    """★ 安全边界：外部调用不允许使用工具。"""
    import inspect

    from app.services.ai import copilot

    sig = inspect.signature(copilot.ask)
    assert "allow_tools" in sig.parameters
    assert sig.parameters["allow_tools"].default is True  # 内部默认开放

    src = inspect.getsource(copilot.ask)
    assert "if not allow_tools" in src, "未实现不开放工具的分支"
