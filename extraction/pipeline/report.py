"""Cost and latency roll-ups. The per-page files are the source of truth; the
summary.json written next to them is a convenience copy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.pdf import open_catalog
from pipeline.store import OutputStore


@dataclass
class CatalogSummary:
    catalog: str
    pdf_pages: int
    pages_ok: int
    pages_error: int
    pages_pending: int
    attempts: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    render_s: float
    api_s: float
    total_s: float
    mean_page_s: float
    max_page_s: float


def _attempts(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Every recorded attempt for a page: earlier failed runs, then the current one."""
    past = [h["metrics"] for h in record.get("history", []) if "metrics" in h]
    return past + ([record["metrics"]] if "metrics" in record else [])


def summarize(store: OutputStore, catalog_dir: Path, catalog: str) -> tuple[CatalogSummary, list[dict[str, Any]]]:
    with open_catalog(catalog_dir, catalog) as doc:
        pdf_pages = doc.page_count
    records = store.iter_pages(catalog)
    attempts = [m for record in records for m in _attempts(record)]
    finals = [record["metrics"] for record in records if "metrics" in record]
    pages_ok = sum(record.get("status") == "ok" for record in records)
    # Batch pages have no per-call latency (they wait in the queue), so only sync pages count.
    page_times = [m["total_s"] for m in finals if m.get("mode", "sync") == "sync"]

    summary = CatalogSummary(
        catalog=catalog,
        pdf_pages=pdf_pages,
        pages_ok=pages_ok,
        pages_error=sum(record.get("status") == "error" for record in records),
        pages_pending=max(0, pdf_pages - pages_ok),
        attempts=sum(m["attempts"] for m in attempts),
        input_tokens=sum(m["input_tokens"] for m in attempts),
        output_tokens=sum(m["output_tokens"] for m in attempts),
        cost_usd=round(sum(m["cost_usd"] for m in attempts), 6),
        render_s=round(sum(m["render_s"] for m in attempts), 2),
        api_s=round(sum(m["api_s"] for m in attempts), 2),
        total_s=round(sum(m["total_s"] for m in attempts), 2),
        mean_page_s=round(sum(page_times) / len(page_times), 2) if page_times else 0.0,
        max_page_s=round(max(page_times), 2) if page_times else 0.0,
    )
    return summary, records


_CATALOG_HEADER = (f"{'catalog':<30}{'pdf':>5}{'ok':>5}{'err':>5}{'todo':>6}{'tries':>7}"
                   f"{'in_tok':>10}{'out_tok':>10}{'cost $':>10}{'avg s':>8}{'max s':>8}{'total s':>10}")


def _catalog_row(s: CatalogSummary, label: str | None = None, totals: bool = False) -> str:
    avg = "" if totals else f"{s.mean_page_s:>8.1f}"
    mx = "" if totals else f"{s.max_page_s:>8.1f}"
    return (f"{label or s.catalog[:29]:<30}{s.pdf_pages:>5}{s.pages_ok:>5}{s.pages_error:>5}"
            f"{s.pages_pending:>6}{s.attempts:>7}{s.input_tokens:>10}{s.output_tokens:>10}"
            f"{s.cost_usd:>10.4f}{avg:>8}{mx:>8}{s.total_s:>10.1f}")


def _grand_total(summaries: list[CatalogSummary]) -> CatalogSummary:
    return CatalogSummary(
        catalog="TOTAL",
        pdf_pages=sum(s.pdf_pages for s in summaries),
        pages_ok=sum(s.pages_ok for s in summaries),
        pages_error=sum(s.pages_error for s in summaries),
        pages_pending=sum(s.pages_pending for s in summaries),
        attempts=sum(s.attempts for s in summaries),
        input_tokens=sum(s.input_tokens for s in summaries),
        output_tokens=sum(s.output_tokens for s in summaries),
        cost_usd=round(sum(s.cost_usd for s in summaries), 6),
        render_s=round(sum(s.render_s for s in summaries), 2),
        api_s=round(sum(s.api_s for s in summaries), 2),
        total_s=round(sum(s.total_s for s in summaries), 2),
        mean_page_s=0.0,
        max_page_s=0.0,
    )


def format_catalogs(summaries: list[CatalogSummary]) -> str:
    lines = [_CATALOG_HEADER] + [_catalog_row(s) for s in summaries]
    if len(summaries) > 1:
        lines.append(_catalog_row(_grand_total(summaries), label="TOTAL", totals=True))
    return "\n".join(lines)


_PAGE_HEADER = (f"{'page':>5}  {'status':<7}{'tries':>6}{'in_tok':>9}{'out_tok':>9}{'cost $':>10}"
                f"{'render':>8}{'api s':>8}{'total s':>9}{'prods':>6}  error")


def format_pages(records: list[dict[str, Any]]) -> str:
    lines = [_PAGE_HEADER]
    for record in sorted(records, key=lambda r: r["page"]):
        m = record.get("metrics", {})
        lines.append(
            f"{record['page']:>5}  {record['status']:<7}{m.get('attempts', 0):>6}"
            f"{m.get('input_tokens', 0):>9}{m.get('output_tokens', 0):>9}{m.get('cost_usd', 0.0):>10.4f}"
            f"{m.get('render_s', 0.0):>8.2f}{m.get('api_s', 0.0):>8.2f}{m.get('total_s', 0.0):>9.2f}"
            f"{len(record.get('products', [])):>6}  {(record.get('error') or '')[:60]}"
        )
    return "\n".join(lines)


def batch_wall_time(manifest: dict[str, Any]) -> timedelta | None:
    """Anthropic's created-to-ended time for a batch, or None until the batch has ended."""
    created, ended = manifest.get("batch_created_at"), manifest.get("batch_ended_at")
    if not created or not ended:
        return None
    return datetime.fromisoformat(ended) - datetime.fromisoformat(created)


def _short_stamp(stamp: str | None) -> str:
    return stamp.replace("T", " ")[:16] if stamp else "-"


def format_batches(manifests: list[dict[str, Any]]) -> str:
    lines = [f"{'batch':<34}{'submission':<12}{'requests':>9}  {'status':<12}{'created (UTC)':<18}"
             f"{'ended (UTC)':<18}{'wall':>10}  collected"]
    for manifest in manifests:
        wall = batch_wall_time(manifest)
        lines.append(
            f"{manifest['batch_id']:<34}{str(manifest.get('submission_id', ''))[:10]:<12}"
            f"{len(manifest['requests']):>9}  {manifest.get('processing_status', '-'):<12}"
            f"{_short_stamp(manifest.get('batch_created_at')):<18}"
            f"{_short_stamp(manifest.get('batch_ended_at')):<18}"
            f"{str(wall) if wall is not None else '-':>10}  {'yes' if manifest['collected'] else 'no'}"
        )
    return "\n".join(lines)
