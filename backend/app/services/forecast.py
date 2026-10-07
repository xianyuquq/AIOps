"""趋势预测。

用途：回答运维最关心的那类问题——「照这个速度，还有多久出事」。

做法是**最小二乘线性拟合**，而不是引入预测库：
- 指标在短时间内基本是线性增长（磁盘、库容量、连接数），线性足够用
- 可解释：斜率就是「每天涨多少」，用户能自己校对
- 无额外依赖，结果可复现

诚实地给出置信度：样本太少、波动太大（R² 低）时不硬给结论，
而是标为「不可靠」。宁可不预测，也不要给一个会误导人的数字。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass
class Fit:
    """线性拟合结果：value = intercept + slope * hours_from_start"""

    ok: bool
    slope_per_hour: float = 0.0
    intercept: float = 0.0
    r2: float = 0.0
    samples: int = 0
    first_value: float = 0.0
    last_value: float = 0.0
    span_hours: float = 0.0
    reason: str = ""
    starts_at: datetime | None = None

    def predict(self, hours_ahead: float) -> float:
        return self.intercept + self.slope_per_hour * (self.span_hours + hours_ahead)


@dataclass
class Forecast:
    """一条可展示的预测结论。"""

    target_type: str          # server | database
    target_id: int
    target_name: str
    metric_key: str
    label: str
    unit: str = ""
    current: float = 0.0
    slope_per_hour: float = 0.0
    slope_per_day: float = 0.0
    r2: float = 0.0
    samples: int = 0
    span_hours: float = 0.0
    limit: float | None = None
    hours_to_limit: float | None = None
    eta: str = ""
    trend: str = "stable"     # rising | falling | stable
    confidence: str = "low"   # high | medium | low
    note: str = ""
    mode: str = "growth"      # growth（外推）| peak（看峰值余量）
    peak: float | None = None
    headroom_pct: float | None = None
    history: list[dict] = field(default_factory=list)
    projection: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "metric_key": self.metric_key,
            "label": self.label,
            "unit": self.unit,
            "current": round(self.current, 2),
            "slope_per_hour": round(self.slope_per_hour, 4),
            "slope_per_day": round(self.slope_per_day, 3),
            "r2": round(self.r2, 3),
            "samples": self.samples,
            "span_hours": round(self.span_hours, 2),
            "limit": self.limit,
            "hours_to_limit": (
                round(self.hours_to_limit, 1) if self.hours_to_limit is not None else None
            ),
            "eta": self.eta,
            "trend": self.trend,
            "confidence": self.confidence,
            "note": self.note,
            "mode": self.mode,
            "peak": round(self.peak, 2) if self.peak is not None else None,
            "headroom_pct": self.headroom_pct,
            "history": self.history,
            "projection": self.projection,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def linear_fit(points: list[tuple[datetime, float]]) -> Fit:
    """对 (时间, 值) 做最小二乘线性拟合。

    返回斜率（单位：每小时）与 R²（拟合优度，用于置信度）。
    """
    if len(points) < 3:
        return Fit(ok=False, samples=len(points), reason="样本不足（至少需要 3 个点）")

    points = sorted(points, key=lambda p: p[0])
    t0 = points[0][0]
    xs = [(ts - t0).total_seconds() / 3600.0 for ts, _ in points]
    ys = [v for _, v in points]

    span = xs[-1] - xs[0]
    if span <= 0:
        return Fit(ok=False, samples=len(points), reason="时间跨度为零")

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return Fit(ok=False, samples=n, reason="时间点重合，无法拟合")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))

    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    # R²：说明「变化里有多少能被这条直线解释」
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return Fit(
        ok=True,
        slope_per_hour=slope,
        intercept=intercept,
        r2=max(0.0, min(1.0, r2)),
        samples=n,
        first_value=ys[0],
        last_value=ys[-1],
        span_hours=span,
        starts_at=t0,
    )


def _confidence(fit: Fit) -> str:
    """根据样本量与拟合优度判断可信度。"""
    if fit.samples >= 20 and fit.r2 >= 0.75:
        return "high"
    if fit.samples >= 8 and fit.r2 >= 0.4:
        return "medium"
    return "low"


def _format_eta(hours: float | None) -> str:
    if hours is None:
        return ""
    if hours < 0:
        return "已超过阈值"
    if hours < 1:
        return f"约 {max(int(hours * 60), 1)} 分钟后"
    if hours < 48:
        return f"约 {hours:.1f} 小时后"
    days = hours / 24
    if days < 365:
        return f"约 {days:.1f} 天后"
    return f"约 {days / 365:.1f} 年后"


def build_forecast(
    points: list[tuple[datetime, float]],
    *,
    target_type: str,
    target_id: int,
    target_name: str,
    metric_key: str,
    label: str,
    unit: str = "",
    limit: float | None = None,
    horizon_hours: float = 24.0,
    min_slope_ratio: float = 0.0005,
    mode: str = "growth",
) -> Forecast | None:
    """由历史点构建一条预测。

    min_slope_ratio：斜率相对于当前值的比例下限。
    变化过于平缓的指标不展示——否则大盘会被「预计 3000 年后写满」之类的噪声占满。
    """
    fit = linear_fit(points)
    current = points[-1][1] if points else 0.0

    fc = Forecast(
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        metric_key=metric_key,
        label=label,
        unit=unit,
        current=current,
        samples=fit.samples,
        limit=limit,
    )
    fc.mode = mode

    if not fit.ok:
        fc.note = fit.reason
        fc.confidence = "low"
        fc.trend = "stable"
        return fc if len(points) >= 2 else None

    fc.slope_per_hour = fit.slope_per_hour
    fc.slope_per_day = fit.slope_per_hour * 24
    fc.r2 = fit.r2
    fc.span_hours = fit.span_hours
    fc.confidence = _confidence(fit)

    # 供图表叠加：历史点（两种模式都要）
    fc.history = [
        {"ts": ts.isoformat(), "value": round(v, 3)} for ts, v in sorted(points)
    ]

    # ---------- 峰值模式：波动型指标不做外推 ----------
    # 连接数在 [0, 上限] 之间来回跳，线性外推会得出
    # 「37 分钟后连接数耗尽」这种吓人但毫无意义的结论。
    # 对这类指标应该看窗口内峰值离上限还有多少余量。
    # 注意：这一段必须在下面的「增长模式阈值判断」**之前**返回，
    # 否则会先被当成增长型算出 hours_to_limit，结论就自相矛盾了。
    if mode == "peak":
        values = [v for _, v in points]
        fc.peak = max(values) if values else current
        fc.trend = "rising"
        if limit:
            headroom = (limit - fc.peak) / limit * 100
            fc.headroom_pct = round(headroom, 1)
            if fc.peak >= limit:
                fc.eta = "已达上限"
                fc.note = "窗口内峰值已达连接上限"
            elif headroom <= 10:
                fc.eta = f"余量仅 {headroom:.1f}%"
                fc.note = f"窗口内峰值 {fc.peak:.0f}／上限 {limit:.0f}"
            else:
                fc.eta = ""
                fc.note = (
                    f"窗口内峰值 {fc.peak:.0f}／上限 {limit:.0f}（余量 {headroom:.0f}%）"
                )
        else:
            fc.eta = ""
            fc.note = f"窗口内峰值 {fc.peak:.0f}"
        return fc

    # ---------- 增长模式：线性外推 ----------
    # 趋势方向：用「一天的变化量」相对于当前值判断，避免浮点噪声
    scale = abs(current) if abs(current) > 1e-9 else 1.0
    daily_ratio = abs(fc.slope_per_day) / scale
    if daily_ratio < min_slope_ratio:
        fc.trend = "stable"
    else:
        fc.trend = "rising" if fc.slope_per_hour > 0 else "falling"

    if limit is not None:
        if current >= limit:
            fc.hours_to_limit = 0.0
            fc.eta = "已达上限"
        elif fc.slope_per_hour > 0:
            fc.hours_to_limit = (limit - current) / fc.slope_per_hour
            # 拟合不可信时不给 ETA —— 宁可不说，也不要给一个会引发误判的数字
            if fc.confidence == "low":
                fc.eta = ""
                fc.note = "波动较大，剩余时间不可靠（仅供参考）"
            else:
                fc.eta = _format_eta(fc.hours_to_limit)
        else:
            fc.hours_to_limit = None
            fc.eta = "当前速率下不会达到"

    if fc.trend != "stable" and fit.starts_at is not None:
        last_ts = max(ts for ts, _ in points)
        steps = 8
        for i in range(1, steps + 1):
            ahead = horizon_hours * i / steps
            fc.projection.append(
                {
                    "ts": (last_ts + timedelta(hours=ahead)).isoformat(),
                    "value": round(fit.predict(ahead), 3),
                }
            )
    return fc


# --------------------------------------------------------------------------- 指标字典

# 指标字典：key -> (显示名, 单位, 阈值, 模式)
#
# 模式决定用什么方式给出结论，这一点很关键：
#   growth —— 单调增长型（磁盘、容量）。线性外推有意义，可以给「还有多久写满」。
#   peak   —— 波动型（如连接数）。它在 [0, 上限] 之间来回跳，
#             线性外推会得出「37 分钟后连接数耗尽」这种吓人但无意义的结论。
#             对这类指标应该看**窗口内的峰值**离上限还有多少余量。
FORECAST_METRICS: dict[str, tuple[str, str, float | None, str]] = {
    "disk.used_percent": ("磁盘使用率", "%", 100.0, "growth"),
    "disk.inode_used_percent": ("inode 使用率", "%", 100.0, "growth"),
    "mem.used_percent": ("内存使用率", "%", 100.0, "growth"),
    "swap.used_percent": ("交换分区使用率", "%", 100.0, "growth"),
    "db.size_mb": ("数据库容量", "MB", None, "growth"),
    "db.rows.total": ("数据行数", "行", None, "growth"),
    # 连接数用峰值模式：上限取实例的 max_connections
    "db.connections.total": ("数据库连接数", "", None, "peak"),
    "db.connections.threads_connected": ("数据库连接数", "", None, "peak"),
    "db.connections.active": ("活跃连接数", "", None, "peak"),
}

# CPU 刻意不纳入：它天然剧烈波动，线性拟合只会产生噪声。
# 需要看 CPU 请用监控图表。
