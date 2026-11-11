"""Cost and tokens per catalogue and per batch, read from the database with SQL aggregates."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pymupdf


def pdf_page_count(path: str) -> int:
    try:
        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception:
        return 0  # PDF moved or unreadable: the report still shows what the database knows


_CATALOG_HEADER = (f"{'catalog':<30}{'pdf':>5}{'ok':>5}{'err':>5}{'wait':>6}{'todo':>6}{'tries':>7}"
                   f"{'in_tok':>10}{'out_tok':>10}{'cost $':>10}{'render s':>10}")


def catalog_rows(stats: list[dict[str, Any]], catalog_dir: Path) -> list[dict[str, Any]]:
    """Add the PDF page count and the pages still to do to each catalog's SQL totals.

    The page count is read from the PDF in the local catalogue folder; without it (for example on a
    machine that only has the stored copy) the PDF column shows 0 and the todo count is not meaningful."""
    rows = []
    for s in stats:
        pdf = Path(catalog_dir) / f"{s['catalog_name']}.pdf"
        pdf_pages = pdf_page_count(str(pdf)) if pdf.is_file() else 0
        rows.append({**s, "pdf_pages": pdf_pages,
                     "todo": max(0, pdf_pages - s["pages_ok"] - s["pages_in_flight"])})
    return rows


def _row(label: str, r: dict[str, Any]) -> str:
    return (f"{label:<30}{r['pdf_pages']:>5}{r['pages_ok']:>5}{r['pages_error']:>5}{r['pages_in_flight']:>6}"
            f"{r['todo']:>6}{r['attempts']:>7}{r['input_tokens']:>10}{r['output_tokens']:>10}"
            f"{float(r['cost_usd']):>10.4f}{float(r['render_s']):>10.1f}")


def format_catalogs(rows: list[dict[str, Any]]) -> str:
    lines = [_CATALOG_HEADER] + [_row(r["catalog_name"][:29], r) for r in rows]
    if len(rows) > 1:
        keys = ("pdf_pages", "pages_ok", "pages_error", "pages_in_flight", "todo", "attempts",
                "input_tokens", "output_tokens", "cost_usd", "render_s")
        lines.append(_row("TOTAL", {k: sum(r[k] for r in rows) for k in keys}))
    return "\n".join(lines)


_PAGE_HEADER = (f"{'page':>5}  {'status':<10}{'tries':>6}{'in_tok':>9}{'out_tok':>9}{'cost $':>10}"
                f"{'render':>8}{'prods':>6}  error")


def format_pages(rows: list[dict[str, Any]]) -> str:
    lines = [_PAGE_HEADER]
    for r in rows:
        lines.append(f"{r['page']:>5}  {r['status']:<10}{r['attempts']:>6}{r['input_tokens']:>9}"
                     f"{r['output_tokens']:>9}{float(r['cost_usd']):>10.4f}{float(r['render_s']):>8.2f}"
                     f"{r['products']:>6}  {(r['error'] or '')[:60]}")
    return "\n".join(lines)


def batch_wall_time(created: datetime | None, ended: datetime | None) -> timedelta | None:
    """Anthropic's created-to-ended time for a batch, or None until the batch has ended."""
    return ended - created if created and ended else None


def wall_suffix(created: datetime | None, ended: datetime | None) -> str:
    wall = batch_wall_time(created, ended)
    return f"  wall={wall}" if wall is not None else ""


def _short_stamp(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M") if moment else "-"


def format_batches(batches: list[dict[str, Any]]) -> str:
    lines = [f"{'batch':<34}{'submission':<12}{'requests':>9}  {'state':<12}{'created (UTC)':<18}"
             f"{'ended (UTC)':<18}{'wall':>10}  collected"]
    for b in batches:
        wall = batch_wall_time(b["batch_created_at"], b["batch_ended_at"])
        lines.append(
            f"{b['anthropic_batch_id']:<34}{b['submission_id'][:10]:<12}{b['count']:>9}  {b['state']:<12}"
            f"{_short_stamp(b['batch_created_at']):<18}{_short_stamp(b['batch_ended_at']):<18}"
            f"{str(wall) if wall is not None else '-':>10}  {'yes' if b['state'] == 'completed' else 'no'}")
    return "\n".join(lines)
