"""
app/services/report/builder.py

Builds the styled HTML ticket report and exports it to PDF.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Dict, List, Tuple

import config as cfg
from app.services.report.charts import (
    bar_chart,
    close_distribution_chart,
    median_close_distribution_chart,
    pie_chart,
)
from app.services.report.pipeline import fmt_duration, priority_level_1_to_6


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


def _img(b64: str, alt: str) -> str:
    if not b64:
        return '<div class="empty-note">No data.</div>'
    return f'<img class="chart-img" src="data:image/png;base64,{b64}" alt="{_esc(alt)}" />'


def _chart_inner(b64: str, title_alt: str, chart_bg_css: str) -> str:
    return f'<div class="chart-box" style="background: {_esc(chart_bg_css)};">{_img(b64, title_alt)}</div>'


def _chart_block(b64: str, section_title: str, chart_bg_css: str) -> str:
    return f"""
    <section class="block">
      <div class="block-title">{_esc(section_title)}</div>
      {_chart_inner(b64, section_title, chart_bg_css)}
    </section>
    """


def _panel(inner_html: str) -> str:
    return f'<div class="pair-panel"><div class="pair-panel-title"></div>{inner_html}</div>'


def _paired_chart_table_block(
    section_title: str,
    b64: str,
    chart_bg: str,
    table_inner_html: str,
    title_bottom_gap_px: int = 10,
) -> str:
    gap_html = f'<div style="height:{int(title_bottom_gap_px)}px;"></div>' if title_bottom_gap_px > 0 else ""
    return f"""
    <section class="block">
    <div class="block-title">{_esc(section_title)}</div>
    {gap_html}
    <div class="pair-grid">
        {_panel(_chart_inner(b64, section_title, chart_bg))}
        {_panel(table_inner_html)}
    </div>
    </section>
    """


def _triple_panel_block(section_title: str, panels_html: List[str]) -> str:
    return f"""
    <section class="block">
      <div class="block-title">{_esc(section_title)}</div>
      <div class="triple-grid">
        {''.join(panels_html)}
      </div>
    </section>
    """


# ── Table builders ────────────────────────────────────────────────────────────

def _simple_table_inner_html(headers: Tuple[str, str], rows: List[Tuple[str, int]]) -> str:
    tr = [f"<tr><th>{_esc(headers[0])}</th><th class='num'>{_esc(headers[1])}</th></tr>"]
    for k, v in rows:
        tr.append(f"<tr><td>{_esc(str(k))}</td><td class='num'>{int(v):,}</td></tr>")
    return f"""
      <div class="table-wrap">
        <table class="table">
          {''.join(tr)}
        </table>
      </div>
    """


def _summary_table_inner_html(data: Dict[str, int], top_n: int = 10) -> str:
    rows = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return _simple_table_inner_html(("Category", "Count"), rows)


def _priority_level_summary_table_inner_html(by_priority: Dict[str, int]) -> str:
    counts = {i: 0 for i in range(1, 7)}
    other_total = 0
    for pname, cnt in (by_priority or {}).items():
        c = int(cnt or 0)
        plevel = priority_level_1_to_6(str(pname or ""), None)
        if plevel in counts:
            counts[plevel] += c
        else:
            other_total += c
    rows: List[Tuple[str, int]] = [(f"Priority {i}", counts[i]) for i in range(1, 7)]
    if other_total:
        rows.append(("Other / Unparsed", other_total))
    return _simple_table_inner_html(("Priority", "Count"), rows)


def _tech_priority_table_html(
    title: str,
    tech_priority_counts: Dict[str, Dict[str, int]],
    top_n: int = cfg.TECH_TABLE_TOP_N,
    inner_only: bool = False,
) -> str:
    items   = sorted(tech_priority_counts.items(), key=lambda kv: kv[1].get("total", 0), reverse=True)[:top_n]
    headers = ["Technician", "Total", "P1", "P2", "P3", "P4", "P5", "P6"]
    if cfg.TECH_TABLE_INCLUDE_OTHER_PRIORITY:
        headers.append("Other")
    th = "".join(
        f"<th>{_esc(h)}</th>" if h == "Technician" else f"<th class='num'>{_esc(h)}</th>"
        for h in headers
    )
    tr = [f"<tr>{th}</tr>"]
    for tech, counts in items:
        cells = [f"<td>{_esc(tech)}</td>"]
        cells.append(f"<td class='num'><strong>{counts.get('total', 0):,}</strong></td>")
        for p in ("p1", "p2", "p3", "p4", "p5", "p6"):
            cells.append(f"<td class='num'>{counts.get(p, 0):,}</td>")
        if cfg.TECH_TABLE_INCLUDE_OTHER_PRIORITY:
            cells.append(f"<td class='num'>{counts.get('other', 0):,}</td>")
        tr.append(f"<tr>{''.join(cells)}</tr>")
    table_html = f"""
      <div class="table-wrap">
        <table class="table">
          {''.join(tr)}
        </table>
      </div>
    """
    if inner_only:
        return table_html
    return f"""
    <section class="block">
      <div class="block-title">{_esc(title)}</div>
      {table_html}
    </section>
    """


# ── Main HTML builder ─────────────────────────────────────────────────────────

def build_report_html(data: dict) -> str:
    """Build the full styled HTML report with embedded base64 charts."""
    params   = data.get("params", {})
    title    = (params.get("report_title")    or "Ticket Report").strip()
    subtitle = (params.get("report_subtitle") or "").strip()
    footer_l = (params.get("footer_left")     or cfg.REPORT_FOOTER_LEFT).strip()
    footer_r = (params.get("footer_right")    or cfg.REPORT_FOOTER_RIGHT).strip()

    date_from = data.get("date_from") or "all"
    date_to   = data.get("date_to")   or "now"
    gen       = data.get("generated", "")
    meta_txt  = f"Generated {gen} \u2014 Tickets entered {date_from} to {date_to}."

    close_secs = data.get("close_seconds", [])
    fc_secs    = data.get("first_contact_seconds", [])

    avg_first = statistics.mean(fc_secs)      if fc_secs    else None
    med_first = statistics.median(fc_secs)    if fc_secs    else None
    avg_close = statistics.mean(close_secs)   if close_secs else None
    med_close = statistics.median(close_secs) if close_secs else None

    kpis = {
        cfg.LBL_TOTAL_TICKETS:     f"{data.get('total', 0):,}",
        cfg.LBL_TOTAL_CLOSED:      f"{data.get('closed', 0):,}",
        cfg.LBL_AVG_FIRST_CONTACT: fmt_duration(avg_first),
        cfg.LBL_MED_FIRST_CONTACT: fmt_duration(med_first),
        cfg.LBL_AVG_CLOSE:         fmt_duration(avg_close),
        cfg.LBL_MED_CLOSE:         fmt_duration(med_close),
    }

    kpi_cards_html = "".join(
        f"""
        <div class="kpi-card">
          <div class="kpi-label">{_esc(label)}</div>
          <div class="kpi-value">{_esc(value)}</div>
        </div>
        """
        for label, value in kpis.items()
    )

    by_board             = data.get("by_board",    {})
    by_status            = data.get("by_status",   {})
    by_priority          = data.get("by_priority", {})
    by_assignee          = data.get("by_assignee", {})
    tech_priority_counts = data.get("tech_priority_counts", {})

    pie_boards_b64   = pie_chart(by_board,    cfg.CHART_BG_PIE_BOARDS)
    pie_status_b64   = pie_chart(by_status,   cfg.CHART_BG_PIE_STATUS)
    pie_priority_b64 = pie_chart(by_priority, cfg.CHART_BG_PIE_PRIORITY)
    bar_assignee_b64 = bar_chart(by_assignee, cfg.CHART_BG_BAR_ASSIGNEE)
    close_dist_b64   = close_distribution_chart(close_secs, cfg.CHART_BG_CLOSE_DIST)
    median_close_b64 = median_close_distribution_chart(close_secs, cfg.CHART_BG_CLOSE_DIST)

    paired_breakdowns_html = "".join([
        "<div style='height:40px;'></div>",
        _paired_chart_table_block(
            "Board Breakdown", pie_boards_b64, cfg.CHART_OUTER_RING_COLOR,
            _summary_table_inner_html(by_board, top_n=10),
        ),
        "<div style='height:40px;'></div>",
        _paired_chart_table_block(
            "Status Breakdown", pie_status_b64, cfg.CHART_OUTER_RING_COLOR,
            _summary_table_inner_html(by_status, top_n=10),
        ),
        "<div style='height:40px;'></div>",
        _triple_panel_block(
            "Priority Breakdown",
            [
                _panel(_chart_inner(pie_priority_b64, "Tickets by Priority", cfg.CHART_OUTER_RING_COLOR)),
                _panel(_priority_level_summary_table_inner_html(by_priority)),
                _panel(_tech_priority_table_html(
                    "Technician Workload by Priority", tech_priority_counts,
                    top_n=cfg.TECH_TABLE_TOP_N, inner_only=True,
                )),
            ],
        ),
    ])

    trends_html = ""
    if cfg.H_TRENDS:
        trends_html += f"\n    <h2>{_esc(cfg.H_TRENDS)}</h2>\n"
    trends_html += _chart_block(close_dist_b64,   "Average Time to Close: Distribution Function",  cfg.CHART_OUTER_RING_COLOR)
    trends_html += _chart_block(median_close_b64, "Median Time to Close: Distribution Function",   cfg.CHART_OUTER_RING_COLOR)

    other_breakdowns_html = ""
    if cfg.H_BREAKDOWNS:
        other_breakdowns_html += f"\n    <h2>{_esc(cfg.H_BREAKDOWNS)}</h2>\n"
    other_breakdowns_html += _chart_block(bar_assignee_b64, "Top Assignees", cfg.CHART_OUTER_RING_COLOR)

    return f"""
    <!doctype html>
    <html>
    <head>
    <meta charset="utf-8" />
    <title>{_esc(title)}</title>
    <style>
        :root {{
        --bg: #ffffff;
        --panel: #101B33;
        --text: #ffffff;
        --muted: #000103;
        --table-head: #152246;
        --table-row: #65b4fd;
        --table-grid: #114488;
        --chart-border: #152246;
        --chart-border-w: 2px;
        --page-pad: 24px;
        --content-w: 930px;
        --block-gap: 14px;
        --radius: 14px;
        }}
        @page {{ size: Letter; margin: 0.7in 0.65in 0.7in 0.65in; }}
        body {{ margin: 0; background: var(--bg); font-family: Arial, Helvetica, sans-serif; color: #0b1220; }}
        .page {{ max-width: var(--content-w); margin: 0 auto; padding: var(--page-pad); }}
        .header {{ margin-bottom: 18px; display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }}
        .header-left {{ min-width: 0; flex: 1 1 auto; }}
        .title {{ font-size: 34px; line-height: 1.05; margin: 0 0 6px 0; color: var(--panel); font-weight: 800; letter-spacing: 0.2px; }}
        .subtitle {{ margin: 0 0 8px 0; color: var(--muted); font-size: 13px; }}
        .meta {{ margin: 0; color: #25304a; font-size: 12px; }}
        h2 {{ margin: 18px 0 10px; font-size: 18px; color: #0b1220; page-break-after: avoid; break-after: avoid; }}
        .kpi-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 10px 0 10px; }}
        .kpi-card {{ background: var(--panel); border: 1px solid #23355F; border-radius: var(--radius); padding: 14px 14px 12px; color: var(--text); page-break-inside: avoid; break-inside: avoid; }}
        .kpi-label {{ font-size: 12px; opacity: 0.95; margin-bottom: 8px; }}
        .kpi-value {{ font-size: 22px; font-weight: 800; }}
        .block {{ margin: 0 0 var(--block-gap) 0; }}
        .block-title {{ font-size: 20px; color: #25304a; margin: 14px 0 8px 0; font-weight: 700; text-transform: none; page-break-after: avoid; break-after: avoid; padding-top: 2px; }}
        .pair-grid {{ display: grid; grid-template-columns: 1.05fr 0.95fr; gap: 10px; align-items: start; }}
        .triple-grid {{ display: grid; grid-template-columns: 0.95fr 0.85fr 1.20fr; gap: 10px; align-items: start; }}
        .pair-panel {{ min-width: 0; }}
        .pair-panel-title {{ font-size: 13px; color: #25304a; margin: 0 0 6px 2px; font-weight: 700; page-break-after: avoid; break-after: avoid; }}
        .chart-box {{ border: var(--chart-border-w) solid var(--chart-border); border-radius: var(--radius); padding: 8px; background: #ffffff; margin-top: 0; page-break-inside: avoid; break-inside: avoid; }}
        .chart-img {{ width: 100%; height: auto; display: block; border-radius: 10px; page-break-inside: avoid; break-inside: avoid; }}
        .empty-note {{ border: 1px dashed #9ab0d6; border-radius: 10px; padding: 18px; color: #4a5d84; font-size: 12px; background: #f7faff; }}
        .table-wrap {{ border: 1px solid #23355F; border-radius: var(--radius); overflow: clip; page-break-inside: avoid !important; break-inside: avoid !important; }}
        table.table {{ width: 100%; border-collapse: collapse; }}
        .table th {{ background: var(--table-head); color: var(--text); font-size: 12px; text-align: left; padding: 10px 10px; border: 1px solid var(--table-grid); }}
        .table td {{ background: var(--table-row); color: var(--text); font-size: 12px; font-weight: 700; padding: 9px 10px; border: 1px solid var(--table-grid); vertical-align: top; }}
        .table tr > *:first-child {{ border-left: none; }}
        .table tr > *:last-child  {{ border-right: none; }}
        .table tr:first-child > * {{ border-top: none; }}
        .table tr:last-child  > * {{ border-bottom: none; }}
        .table td.num, .table th.num {{ font-weight: 700; text-align: right; white-space: nowrap; width: 120px; }}
        .footer {{ margin-top: 18px; padding-top: 10px; border-top: 1px solid #c7d2e8; display: flex; justify-content: space-between; color: #25304a; font-size: 11px; page-break-inside: avoid; break-inside: avoid; }}
        @media print {{
        .page {{ padding: 0; }}
        body {{ background: #ffffff; }}
        .block, .pair-grid, .triple-grid, .pair-panel {{ page-break-inside: auto !important; break-inside: auto !important; }}
        .chart-box, .kpi-card, img, .chart-img {{ page-break-inside: avoid !important; break-inside: avoid !important; }}
        .table-wrap {{ page-break-inside: avoid !important; break-inside: avoid !important; }}
        .block-title, .pair-panel-title, h2 {{ page-break-after: avoid !important; break-after: avoid !important; }}
        .pair-grid, .triple-grid {{ display: block !important; }}
        .pair-panel {{ margin-bottom: 10px; }}
        }}
        @media (max-width: 900px) {{
        .pair-grid {{ grid-template-columns: 1fr; }}
        .triple-grid {{ grid-template-columns: 1fr; }}
        .kpi-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
        }}
    </style>
    </head>
    <body>
    <div class="page">
        <div class="header">
        <div class="header-left">
            <div class="title">{_esc(title)}</div>
            {"<div class='subtitle'>" + _esc(subtitle) + "</div>" if subtitle else ""}
            <p class="meta">{_esc(meta_txt)}</p>
        </div>
        </div>

        <h2>{_esc(cfg.H_KPIS)}</h2>
        <div class="kpi-grid">
        {kpi_cards_html}
        </div>

        {paired_breakdowns_html}

        {trends_html}

        {other_breakdowns_html}

        <div class="footer">
        <div>{_esc(footer_l)}</div>
        <div>{_esc(footer_r)}</div>
        </div>
    </div>
    </body>
    </html>
    """


# ── PDF export ────────────────────────────────────────────────────────────────

def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """
    Convert an HTML file to PDF using Playwright (preferred) or wkhtmltopdf.
    Returns True on success.
    """
    import shutil, subprocess

    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) Playwright (best quality)
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page    = browser.new_page()
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            page.pdf(
                path=str(pdf_path),
                format="Letter",
                print_background=True,
                margin={"top": "0.7in", "right": "0.65in", "bottom": "0.7in", "left": "0.65in"},
            )
            browser.close()
        return True
    except Exception:
        pass

    # 2) wkhtmltopdf fallback
    wk = shutil.which("wkhtmltopdf")
    if wk:
        try:
            subprocess.run(
                [wk, "--enable-local-file-access", str(html_path), str(pdf_path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return True
        except subprocess.CalledProcessError:
            pass

    return False
