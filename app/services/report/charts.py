"""
app/services/report/charts.py

Matplotlib chart generation for the ticket report.
All functions return base64-encoded PNG strings suitable for embedding
in HTML with a data:image/png;base64,... src.
"""

from __future__ import annotations

import base64
import io
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import config as cfg


# ── Internal helpers ──────────────────────────────────────────────────────────

def _apply_chart_theme(fig, ax, ax_bg: str) -> None:
    fig.patch.set_facecolor(cfg.CHART_OUTER_RING_COLOR)
    fig.patch.set_edgecolor(cfg.CHART_BORDER_COLOR)
    fig.patch.set_linewidth(cfg.CHART_BORDER_WIDTH)
    ax.set_facecolor(ax_bg)
    ax.title.set_color(cfg.CHART_TITLE_COLOR)
    ax.tick_params(colors=cfg.CHART_TEXT_COLOR)
    for spine in ax.spines.values():
        spine.set_color(cfg.CHART_TEXT_COLOR)


def _compact_other_bucket(
    data: Dict[str, int], top_n: int
) -> Tuple[List[str], List[int]]:
    items = sorted(data.items(), key=lambda x: x[1], reverse=True)
    head  = items[:top_n]
    tail  = items[top_n:]
    if tail:
        head.append(("Other", sum(v for _, v in tail)))
    return [k for k, _ in head], [v for _, v in head]


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(
        buf,
        format="png",
        dpi=180,
        facecolor=cfg.CHART_OUTER_RING_COLOR,
        edgecolor=cfg.CHART_OUTER_RING_COLOR,
        bbox_inches="tight",
        pad_inches=0.35,
    )
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


# ── Public chart builders ─────────────────────────────────────────────────────

def pie_chart(
    data: Dict[str, int],
    ax_bg: str,
    top_n: int = cfg.PIE_TOP_N_RPT,
    small_pct: float = cfg.PIE_SMALL_SLICE_PCT,
) -> str:
    """Return a base64 PNG of a pie chart, or empty string if no data."""
    if not data:
        return ""
    labels, values = _compact_other_bucket(data, top_n=top_n)
    total = float(sum(values)) if values else 0.0
    if total <= 0:
        return ""

    pcts = [(v / total) * 100.0 for v in values]
    display_for_legend: List[str] = []
    idx_counter = 1
    for lab, pct in zip(labels, pcts):
        safe_lab = (lab[:55] + "\u2026") if len(lab) > 56 else lab
        if pct < small_pct:
            display_for_legend.append(f"[{idx_counter}] {safe_lab} ({pct:.0f}%)")
            idx_counter += 1
        else:
            display_for_legend.append(f"{safe_lab} ({pct:.0f}%)")

    fig, ax = plt.subplots(figsize=(7.4, 3.2))
    _apply_chart_theme(fig, ax, ax_bg=ax_bg)

    def autopct_func(pct: float) -> str:
        return f"{pct:.0f}%" if pct >= small_pct else ""

    wedges, _texts, _autotexts = ax.pie(
        values, labels=None, autopct=autopct_func, startangle=90, pctdistance=0.72,
    )
    for t in _autotexts:
        t.set_color(cfg.CHART_TEXT_COLOR)
        t.set_fontsize(9)
    ax.axis("equal")
    leg = ax.legend(
        wedges, display_for_legend,
        loc="center left", bbox_to_anchor=(0.98, 0.5),
        fontsize=8, frameon=True, borderpad=0.40, labelspacing=0.40,
        handlelength=1.35, handleheight=0.9, handletextpad=0.55, borderaxespad=0.25,
    )
    leg.get_frame().set_facecolor(ax_bg)
    leg.get_frame().set_edgecolor(cfg.CHART_TEXT_COLOR)
    fig.subplots_adjust(right=0.69)
    return _fig_to_b64(fig)


def bar_chart(data: Dict[str, int], ax_bg: str, top_n: int = 10) -> str:
    """Return a base64 PNG horizontal bar chart, or empty string if no data."""
    if not data:
        return ""
    items  = sorted(data.items(), key=lambda x: x[1], reverse=True)[:top_n]
    labels = [k for k, _ in items][::-1]
    values = [v for _, v in items][::-1]

    fig, ax = plt.subplots(figsize=(6.3, 3.6))
    _apply_chart_theme(fig, ax, ax_bg=ax_bg)
    ax.barh(labels, values)
    ax.tick_params(axis="y", labelsize=9, colors=cfg.CHART_TEXT_COLOR)
    ax.tick_params(axis="x", labelsize=9, colors=cfg.CHART_TEXT_COLOR)
    fig.tight_layout(pad=1.2)

    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=180,
        facecolor=cfg.CHART_OUTER_RING_COLOR, edgecolor=cfg.CHART_OUTER_RING_COLOR,
    )
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def close_distribution_chart(
    close_seconds: List[float],
    ax_bg: str,
    bins: int = cfg.CLOSE_DIST_BINS,
    max_days: Optional[float] = cfg.CLOSE_DIST_MAX_DAYS,
) -> str:
    """
    Return a base64 PNG of the time-to-close distribution
    (probability density bars + smoothed count overlay).
    """
    vals_days: List[float] = []
    for s in close_seconds:
        try:
            if s is None or s < 0:
                continue
            d = float(s) / 86400.0
            if max_days is not None:
                d = min(d, float(max_days))
            vals_days.append(d)
        except Exception:
            continue
    if not vals_days:
        return ""

    lo, hi = 0.0, max(vals_days)
    if hi <= lo:
        hi = lo + 1.0
    bins   = max(8, int(bins))
    bin_w  = (hi - lo) / bins
    if bin_w <= 0:
        bin_w = 1.0
    edges  = [lo + i * bin_w for i in range(bins + 1)]
    counts = [0] * bins
    for v in vals_days:
        idx = max(0, min(bins - 1, int((v - lo) / bin_w)))
        counts[idx] += 1
    n       = float(len(vals_days))
    density = [(c / (n * bin_w)) if n > 0 else 0.0 for c in counts]
    centers = [0.5 * (edges[i] + edges[i + 1]) for i in range(bins)]

    def moving_average(vals, window=3):
        if not vals:
            return []
        window = max(1, int(window))
        half = window // 2
        return [
            sum(vals[max(0, i - half):min(len(vals), i + half + 1)]) /
            len(vals[max(0, i - half):min(len(vals), i + half + 1)])
            for i in range(len(vals))
        ]

    def densify_linear(xs, ys, points_per_seg=6):
        if len(xs) < 2:
            return xs[:], ys[:]
        xx, yy = [], []
        pps = max(2, int(points_per_seg))
        for i in range(len(xs) - 1):
            for j in range(pps):
                t2 = j / float(pps)
                xx.append(xs[i] + (xs[i + 1] - xs[i]) * t2)
                yy.append(ys[i] + (ys[i + 1] - ys[i]) * t2)
        xx.append(xs[-1])
        yy.append(ys[-1])
        return xx, yy

    smooth_density = moving_average(density, window=3)
    smooth_counts  = moving_average(counts, window=3)
    dense_x_c, dense_y_c = densify_linear(centers, smooth_counts, points_per_seg=8)

    fig, ax = plt.subplots(figsize=(9.8, 2.8))
    _apply_chart_theme(fig, ax, ax_bg=ax_bg)
    ax.bar(centers, density, width=bin_w * 0.92, alpha=0.70, color=cfg.CLOSE_DIST_BAR_COLOR)
    ax.set_ylabel("Probability density",  color=cfg.CHART_TEXT_COLOR, fontsize=15)
    ax.set_xlabel("Time to close (days)", color=cfg.CHART_TEXT_COLOR, fontsize=15)
    ax2 = ax.twinx()
    ax2.plot(dense_x_c, dense_y_c, linewidth=2.0, color=cfg.CLOSE_DIST_COUNT_SMOOTH_LINE_COLOR)
    ax2.tick_params(colors=cfg.CHART_TEXT_COLOR)
    for spine in ax2.spines.values():
        spine.set_color(cfg.CHART_TEXT_COLOR)
    ax.grid(True, which="major", axis="y", alpha=0.25)
    fig.tight_layout(pad=1.2)

    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=180,
        facecolor=cfg.CHART_OUTER_RING_COLOR, edgecolor=cfg.CHART_OUTER_RING_COLOR,
    )
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def median_close_distribution_chart(
    values_seconds: List[float],
    ax_bg: str = cfg.CHART_BG_CLOSE_DIST,
    bins: int = cfg.CLOSE_DIST_BINS,
    max_days: Optional[float] = cfg.CLOSE_DIST_MAX_DAYS,
) -> str:
    """Return a base64 PNG of the median time-to-close distribution."""
    vals_days: List[float] = []
    for s in values_seconds:
        try:
            if s is None or s < 0:
                continue
            d = float(s) / 86400.0
            if max_days is not None:
                d = min(d, float(max_days))
            vals_days.append(d)
        except Exception:
            continue
    if not vals_days:
        return ""

    lo, hi = 0.0, max(vals_days)
    if hi <= lo:
        hi = lo + 1.0
    bins   = max(12, int(bins))
    bin_w  = (hi - lo) / bins
    if bin_w <= 0:
        bin_w = 1.0
    edges  = [lo + i * bin_w for i in range(bins + 1)]
    counts = [0] * bins
    for v in vals_days:
        idx = max(0, min(bins - 1, int((v - lo) / bin_w)))
        counts[idx] += 1
    n       = float(len(vals_days))
    density = [(c / (n * bin_w)) if n > 0 else 0.0 for c in counts]
    centers = [0.5 * (edges[i] + edges[i + 1]) for i in range(bins)]

    window = cfg.DIST_SMOOTH_WINDOW
    half   = window // 2
    smooth_density = [
        sum(density[max(0, i - half):min(len(density), i + half + 1)]) /
        len(density[max(0, i - half):min(len(density), i + half + 1)])
        for i in range(len(density))
    ]

    pps = cfg.DIST_DENSIFY_POINTS
    dense_x, dense_y = [], []
    for i in range(len(centers) - 1):
        for j in range(pps):
            t2 = j / float(pps)
            dense_x.append(centers[i] + (centers[i + 1] - centers[i]) * t2)
            dense_y.append(smooth_density[i] + (smooth_density[i + 1] - smooth_density[i]) * t2)
    dense_x.append(centers[-1])
    dense_y.append(smooth_density[-1])

    fig, ax = plt.subplots(figsize=(9.8, 3.0))
    _apply_chart_theme(fig, ax, ax_bg=ax_bg)
    ax.bar(centers, density, width=bin_w * 0.92, alpha=0.72, color=cfg.CLOSE_DIST_BAR_COLOR)
    ax.plot(dense_x, dense_y, linewidth=2.2, color=cfg.CLOSE_DIST_COUNT_SMOOTH_LINE_COLOR)
    ax.plot(
        centers, smooth_density,
        linestyle="None", marker="o", markersize=2.6,
        color=cfg.CLOSE_DIST_COUNT_SMOOTH_LINE_COLOR,
    )
    ax.set_ylabel("Probability density",  color=cfg.CHART_TEXT_COLOR, fontsize=12)
    ax.set_xlabel("Time to close (days)", color=cfg.CHART_TEXT_COLOR, fontsize=12)
    ax.tick_params(axis="both", labelsize=9, colors=cfg.CHART_TEXT_COLOR)
    ax.grid(True, which="major", axis="y", alpha=0.25)
    fig.tight_layout(pad=1.2)

    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=180,
        facecolor=cfg.CHART_OUTER_RING_COLOR, edgecolor=cfg.CHART_OUTER_RING_COLOR,
    )
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()
