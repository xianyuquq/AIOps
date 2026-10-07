"""趋势预测的回归测试。

这些用例锁定的都是「预测会误导人」的具体场景——
一个不准的预测比没有预测更糟，所以判据要卡死。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402
from app.services.forecast import (  # noqa: E402
    FORECAST_METRICS,
    build_forecast,
    linear_fit,
)

T0 = datetime(2026, 1, 1, 0, 0, 0)


def series(values: list[float], step_minutes: int = 60) -> list[tuple[datetime, float]]:
    return [(T0 + timedelta(minutes=i * step_minutes), v) for i, v in enumerate(values)]


def make(points, **kw):
    base = dict(
        target_type="server", target_id=1, target_name="t",
        metric_key="disk.used_percent", label="磁盘使用率", unit="%",
    )
    base.update(kw)
    return build_forecast(points, **base)


# ============================================================ 拟合本身


def test_linear_fit_recovers_known_slope():
    """每小时涨 2 的直线，应当被准确还原。"""
    fit = linear_fit(series([10, 12, 14, 16, 18]))
    assert fit.ok
    assert fit.slope_per_hour == pytest.approx(2.0, abs=1e-6)
    assert fit.r2 == pytest.approx(1.0, abs=1e-6)


def test_linear_fit_needs_enough_points():
    fit = linear_fit(series([1, 2]))
    assert not fit.ok
    assert "样本不足" in fit.reason


def test_linear_fit_flat_series():
    fit = linear_fit(series([5, 5, 5, 5]))
    assert fit.ok
    assert fit.slope_per_hour == pytest.approx(0.0, abs=1e-9)


def test_linear_fit_rejects_identical_timestamps():
    pts = [(T0, 1.0), (T0, 2.0), (T0, 3.0)]
    fit = linear_fit(pts)
    assert not fit.ok


# ============================================================ 增长型


def test_growth_forecast_computes_eta_to_limit():
    """磁盘每小时涨 1%，当前 19%，距 100% 还有 81 小时。"""
    pts = series([10 + i for i in range(10)])   # 10..19，斜率 1/h
    fc = make(pts, limit=100.0)
    assert fc is not None
    assert fc.trend == "rising"
    assert fc.hours_to_limit == pytest.approx(81.0, abs=1.0)
    assert fc.eta, "高/中可信度的趋势应当给出剩余时间"


def test_growth_forecast_days_formatting():
    """剩余时间超过 2 天时应以「天」为单位展示，而不是一串小时数。"""
    pts = series([10 + i for i in range(10)])   # 斜率 1/h → 约 81 小时
    fc = make(pts, limit=100.0)
    assert fc.hours_to_limit is not None and fc.hours_to_limit > 48
    assert "天" in fc.eta, f"未按天展示：{fc.eta}"


def test_growth_forecast_hours_formatting_when_soon():
    """不足 2 天时用「小时」更直观。"""
    pts = [(T0 + timedelta(hours=i), 50.0 + i * 2) for i in range(10)]  # 2%/h → 25h
    fc = make(pts, limit=100.0)
    assert fc.hours_to_limit is not None and fc.hours_to_limit < 48
    assert "小时" in fc.eta, f"未按小时展示：{fc.eta}"


def test_no_limit_means_no_eta():
    """容量类指标没有固定上限，只给增长率。"""
    pts = series([100 + i * 10 for i in range(8)])
    fc = make(pts, metric_key="db.size_mb", label="数据库容量", unit="MB", limit=None)
    assert fc.trend == "rising"
    assert fc.slope_per_day == pytest.approx(240.0, abs=1)
    assert fc.eta == ""


def test_flat_metric_marked_stable():
    """完全平的指标不应被当成趋势，否则大盘全是噪声。"""
    fc = make(series([42.0] * 12), limit=100.0)
    assert fc.trend == "stable"


def test_falling_metric_not_rising():
    fc = make(series([90 - i for i in range(10)]), limit=100.0)
    assert fc.trend == "falling"


def test_already_over_limit():
    fc = make(series([101 + i for i in range(5)]), limit=100.0)
    assert fc.eta == "已达上限"


# ============================================================ 可信度


def test_low_confidence_suppresses_eta():
    """★ 拟合不可信时不给剩余时间 —— 宁可不说，也不要给误导性数字。"""
    noisy = [10, 90, 12, 88, 11, 89, 13, 87]
    fc = make(series([float(v) for v in noisy]), limit=100.0)
    assert fc.confidence == "low"
    if fc.slope_per_hour > 0:
        assert fc.eta == "", "低可信度却给出了剩余时间"
        assert "不可靠" in fc.note


def test_high_confidence_for_clean_trend():
    fc = make(series([float(10 + i) for i in range(25)]), limit=100.0)
    assert fc.confidence == "high"
    assert fc.eta != ""


def test_r2_in_range():
    for vals in ([1, 5, 2, 9, 3], [1, 2, 3, 4, 5], [5, 5, 5, 5, 5]):
        fit = linear_fit(series([float(v) for v in vals]))
        if fit.ok:
            assert 0.0 <= fit.r2 <= 1.0


# ============================================================ 波动型（峰值模式）


def test_peak_mode_does_not_extrapolate_connections():
    """★ 连接数波动，线性外推会得出「马上耗尽」的误导结论。

    这里刻意构造一个线性上升的连接数序列（会算出很短的 ETA），
    验证峰值模式**不会**给出这种 ETA。
    """
    pts = series([float(5 + i) for i in range(10)])   # 5..14，线性上升
    fc = make(pts, metric_key="db.connections.total", label="数据库连接数",
              unit="", limit=10.0, mode="peak")
    assert fc.mode == "peak"
    assert fc.hours_to_limit is None, "峰值模式不应给出剩余时间"
    assert fc.peak == 14.0
    assert fc.eta in ("已达上限",) or fc.headroom_pct is not None


def test_peak_mode_reports_headroom():
    pts = series([5.0, 7.0, 6.0, 8.0, 7.0, 6.0])
    fc = make(pts, metric_key="db.connections.total", label="数据库连接数",
              unit="", limit=100.0, mode="peak")
    assert fc.peak == 8.0
    assert fc.headroom_pct == pytest.approx(92.0, abs=0.1)
    assert "余量" in fc.note


def test_peak_mode_warns_when_almost_full():
    pts = series([9.0, 9.0, 10.0, 9.0])
    fc = make(pts, metric_key="db.connections.total", label="数据库连接数",
              unit="", limit=10.0, mode="peak")
    assert fc.eta == "已达上限"


def test_peak_mode_no_projection_points():
    pts = series([1.0, 2.0, 3.0, 4.0])
    fc = make(pts, metric_key="db.connections.total", label="连接数",
              unit="", limit=10.0, mode="peak")
    assert fc.projection == [], "峰值模式不应画外推线"


# ============================================================ 指标字典


def test_database_connection_metrics_use_peak_mode():
    """★ 连接数必须走峰值模式。"""
    for key in ("db.connections.total", "db.connections.threads_connected"):
        assert FORECAST_METRICS[key][3] == "peak"


def test_capacity_metrics_use_growth_mode():
    for key in ("disk.used_percent", "mem.used_percent", "db.size_mb"):
        assert FORECAST_METRICS[key][3] == "growth"


def test_cpu_excluded_from_forecast():
    """CPU 天然剧烈波动，不做预测，避免噪声。"""
    assert "cpu.usage_percent" not in FORECAST_METRICS


# ============================================================ 接口


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/auth/login/json",
            json={"username": "admin", "password": "admin123"},
        )
        c.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
        yield c


def test_summary_has_forecasts_field(client):
    s = client.get("/api/v1/dashboard/summary").json()
    assert "forecasts" in s
    assert isinstance(s["forecasts"], list)


def test_forecast_items_have_required_fields(client):
    s = client.get("/api/v1/dashboard/summary").json()
    for f in s["forecasts"]:
        for field in (
            "target_type", "target_id", "target_name", "metric_key", "label",
            "current", "slope_per_day", "r2", "confidence", "eta", "mode",
        ):
            assert field in f, f"预测项缺少字段 {field}"
        assert f["confidence"] in ("high", "medium", "low")
        assert f["mode"] in ("growth", "peak")


def test_all_forecasts_are_rising(client):
    """大盘只展示上升趋势——下降对运维一般不是风险。"""
    s = client.get("/api/v1/dashboard/summary").json()
    for f in s["forecasts"]:
        assert f["trend"] == "rising"


def test_forecast_endpoint_404_for_unknown_target(client):
    r = client.get(
        "/api/v1/metrics/forecast",
        params={"metric_key": "db.size_mb", "target_type": "database",
                "target_id": 999999},
    )
    assert r.status_code == 404
