"""Production benchmarks: `python -m pipeline benchmark`.

Three kinds of number, kept apart in the output because they are not equally trustworthy:

  measured   read straight from the database: cost, latency, success, retries, volumes
  reviewed   product-table edits by reviewers, compared with the immutable raw model output
  audited    ground truth from a human audit sheet (`--export-audit`, fill in, then `--audit`)

Accuracy and fabrication cannot be measured without a person checking pages against the PDF;
the audit sheet is the cheapest way to get them. No API calls are made."""
from __future__ import annotations

import csv
import json
import math
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

from pipeline.records import product_row
from pipeline.repo import Repo

LOW_CONFIDENCE = 0.8
_NAMES = "(%(names)s::text[] is null or c.catalog_name = any(%(names)s))"
_RESOLVED = "(a.error is not null or a.input_tokens > 0)"   # an attempt whose outcome is known
_COMPARED = ("product_name", "product_description", "variant", "price_amount", "price_currency",
             "tax_note", "weight", "weight_unit")
PRICE_CHECKS = ("correct", "wrong", "fabricated", "missing", "none")
AUDIT_COLUMNS = ("catalog", "page", "pdf_path", "position", "product_name", "price_amount",
                 "price_currency", "variant", "weight", "confidence",
                 "name_correct", "price_check", "missed_on_page", "notes")


# Statistics helpers

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% interval for a proportion k/n. Honest about small samples, unlike k/n alone."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def rate(k: int, n: int) -> dict[str, Any]:
    interval = wilson(k, n)
    return {"k": k, "n": n, "rate": k / n if n else None,
            "ci_low": interval[0] if interval else None, "ci_high": interval[1] if interval else None}


# Measured: straight from the database

def measured(repo: Repo, catalogs: list[str] | None) -> dict[str, Any]:
    params = {"model": repo.model, "names": catalogs or None}
    pages = repo.fetch(
        f"""select c.catalog_name,
                   count(*) filter (where p.status = 'ok') as ok,
                   count(*) filter (where p.status = 'error') as error,
                   count(*) filter (where p.status in ('reserved', 'submitted')) as in_flight,
                   count(*) filter (where p.status = 'ok' and p.page_type = 'product') as product_pages,
                   count(*) filter (where p.status = 'ok' and p.page_type = 'non_product') as non_product_pages
            from page_extractions p join catalog c using (catalog_id)
            where p.model = %(model)s and {_NAMES} group by c.catalog_name order by 1""", params)
    attempts = repo.fetch(
        f"""select c.catalog_name, count(*) as attempts,
                   count(*) filter (where a.error is not null) as failed_attempts,
                   coalesce(sum(a.cost_usd), 0) as cost,
                   coalesce(sum(a.cost_usd) filter (where a.error is not null), 0) as wasted_cost,
                   coalesce(sum(a.input_tokens), 0) as input_tokens,
                   coalesce(sum(a.output_tokens), 0) as output_tokens,
                   count(distinct a.extraction_id) as pages_tried,
                   count(distinct a.extraction_id) filter (where a.attempt_no > 1) as pages_retried
            from extraction_attempts a join page_extractions p using (extraction_id)
                 join catalog c using (catalog_id)
            where p.model = %(model)s and {_NAMES} and {_RESOLVED} group by c.catalog_name""", params)
    products = repo.fetch(
        f"""select c.catalog_name, count(*) as products,
                   count(*) filter (where d.price_amount is null) as no_price,
                   count(*) filter (where d.confidence < {LOW_CONFIDENCE}) as low_confidence
            from product d join page_extractions p using (extraction_id) join catalog c using (catalog_id)
            where p.model = %(model)s and {_NAMES} group by c.catalog_name""", params)
    states = {r["state"]: r["n"] for r in repo.fetch(
        "select state, count(*) as n from batch_submissions group by state")}

    by_catalog: dict[str, dict[str, Any]] = {}
    for table in (pages, attempts, products):
        for row in table:
            by_catalog.setdefault(row["catalog_name"], {}).update(
                {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items() if k != "catalog_name"})
    totals: dict[str, float] = {}
    for row in by_catalog.values():
        for key, value in row.items():
            totals[key] = totals.get(key, 0) + value

    walls, throughput = [], []
    for b in repo.all_batches():
        if b["batch_created_at"] and b["batch_ended_at"]:
            seconds = (b["batch_ended_at"] - b["batch_created_at"]).total_seconds()
            walls.append(seconds)
            if seconds > 0:
                throughput.append(b["count"] / (seconds / 3600))
    return {"catalogs": by_catalog, "totals": totals, "batch_walls_s": walls, "batch_pages_per_hour": throughput,
            "submission_states": states}


# Reviewed: what reviewers changed in `product`, compared with the raw model output

def _norm(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (Decimal, float, int)) and not isinstance(value, bool):
        return round(float(value), 3)
    return value.strip() if isinstance(value, str) else value


def reviewed(repo: Repo, catalogs: list[str] | None) -> dict[str, Any]:
    """Only catalogues with level-1 review done count. Compares each product row to the raw output
    row at the same position: changed fields, deleted rows, and rows the reviewer added."""
    params = {"model": repo.model, "names": catalogs or None}
    pages = repo.fetch(
        f"""select p.extraction_id, d.raw_output from page_extractions p
            join extraction_data d using (extraction_id) join catalog c using (catalog_id)
            join review r on r.catalog_id = c.catalog_id and r.level_1_at is not null
            where p.model = %(model)s and {_NAMES}""", params)
    if not pages:
        return {"pages": 0}
    ids = [p["extraction_id"] for p in pages]
    rows: dict[int, dict[int, dict[str, Any]]] = {}
    for r in repo.fetch("select * from product where extraction_id = any(%s)", (ids,)):
        rows.setdefault(r["extraction_id"], {})[r["position"]] = r

    fields = changed_fields = changed_rows = matched = deleted = added = raw_total = 0
    by_field = dict.fromkeys(_COMPARED, 0)
    for page in pages:
        raw = page["raw_output"].get("products", [])
        edited = rows.get(page["extraction_id"], {})
        raw_total += len(raw)
        for position, product in enumerate(raw, start=1):
            if position not in edited:
                deleted += 1
                continue
            matched += 1
            original = product_row(product, position)
            diff = [f for f in _COMPARED if _norm(original[f]) != _norm(edited[position][f])]
            fields += len(_COMPARED)
            changed_fields += len(diff)
            changed_rows += bool(diff)
            for f in diff:
                by_field[f] += 1
        added += sum(1 for pos in edited if pos > len(raw))
    return {"pages": len(pages), "raw_products": raw_total, "matched": matched, "deleted": deleted, "added": added,
            "field_correction": rate(changed_fields, fields), "product_correction": rate(changed_rows, matched),
            "by_field": by_field}


# Audited: ground truth from the audit sheet

def export_audit(repo: Repo, path: Path, size: int, seed: int, catalogs: list[str] | None) -> tuple[int, int]:
    """Write a random sample of ok pages (product and non-product, so missed products can be found).
    Returns (pages, product rows). Never overwrites an existing sheet: it may hold hours of work."""
    if path.exists():
        raise FileExistsError(f"{path} already exists; move it or pick another name")
    params = {"model": repo.model, "names": catalogs or None}
    pages = repo.fetch(
        f"""select p.extraction_id, p.page, c.catalog_name, d.raw_output
            from page_extractions p join catalog c using (catalog_id) join extraction_data d using (extraction_id)
            where p.model = %(model)s and p.status = 'ok' and {_NAMES}""", params)
    if not pages:
        raise RuntimeError("no extracted pages to sample yet: run `python -m pipeline batch sync` until "
                           "the batches are collected, then export the audit sheet")
    rng = random.Random(seed)
    by_catalog: dict[str, list[dict[str, Any]]] = {}
    for p in pages:
        by_catalog.setdefault(p["catalog_name"], []).append(p)
    sample: list[dict[str, Any]] = []
    for name, group in sorted(by_catalog.items()):
        quota = min(len(group), max(1, round(size * len(group) / len(pages))))
        sample += rng.sample(group, quota)
    sample.sort(key=lambda p: (p["catalog_name"], p["page"]))

    count = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel reads UTF-8
        out = csv.DictWriter(fh, fieldnames=AUDIT_COLUMNS)
        out.writeheader()
        for p in sample:
            base = {"catalog": p["catalog_name"], "page": p["page"], "pdf_path": f"{p['catalog_name']}.pdf"}
            products = p["raw_output"].get("products", [])
            if not products:
                out.writerow(base)  # a page the model found nothing on: the auditor fills missed_on_page
                continue
            for position, product in enumerate(products, start=1):
                row = product_row(product, position)
                out.writerow({**base, "position": position, "product_name": row["product_name"],
                              "price_amount": "" if row["price_amount"] is None else row["price_amount"],
                              "price_currency": row["price_currency"], "variant": row["variant"] or "",
                              "weight": product.get("weight") or "",
                              "confidence": "" if row["confidence"] is None else row["confidence"]})
                count += 1
    return len(sample), count


def load_audit(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Return audited pages and a list of problems. A page counts as audited once its
    `missed_on_page` cell is filled (use 0 if nothing was missed)."""
    problems: list[str] = []
    grouped: dict[tuple[str, str], list[tuple[int, dict[str, str]]]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for line, row in enumerate(csv.DictReader(fh), start=2):
            grouped.setdefault((row["catalog"], row["page"]), []).append((line, row))
    pages = []
    for (catalog, page), rows in grouped.items():
        missed_cells = [(ln, r["missed_on_page"].strip()) for ln, r in rows if r["missed_on_page"].strip()]
        if not missed_cells:
            continue  # not audited yet
        if len(missed_cells) > 1 or not missed_cells[0][1].isdigit():
            problems.append(f"{catalog} p{page}: missed_on_page must be one whole number")
            continue
        products = []
        for line, r in rows:
            if not r["position"].strip():
                continue
            name = r["name_correct"].strip().lower()
            check = r["price_check"].strip().lower()
            priced = bool(r["price_amount"].strip())
            if name not in ("y", "n"):
                problems.append(f"line {line}: name_correct must be y or n")
                continue
            if name == "y" and check not in PRICE_CHECKS:
                problems.append(f"line {line}: price_check must be one of {', '.join(PRICE_CHECKS)}")
                continue
            if (check in ("fabricated", "correct", "wrong") and not priced) or (check in ("missing", "none") and priced):
                problems.append(f"line {line}: price_check '{check}' contradicts the extracted price")
                continue
            confidence = r["confidence"].strip()
            products.append({"name_ok": name == "y", "price_check": check if name == "y" else None,
                             "priced": priced, "confidence": float(confidence) if confidence else None})
        pages.append({"catalog": catalog, "page": page, "missed": int(missed_cells[0][1]), "products": products})
    return pages, problems


def audited(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Accuracy numbers from audited pages. All rates carry n and a 95% interval."""
    rows = [p for page in pages for p in page["products"]]
    extracted, right = len(rows), sum(r["name_ok"] for r in rows)
    missed = sum(page["missed"] for page in pages)
    checks = [r["price_check"] for r in rows if r["price_check"]]
    count = {c: checks.count(c) for c in PRICE_CHECKS}
    truth_priced = count["correct"] + count["wrong"] + count["missing"]
    given = count["correct"] + count["wrong"] + count["fabricated"]
    precision, recall = rate(right, extracted), rate(right, right + missed)
    f1 = None
    if precision["rate"] and recall["rate"]:
        f1 = 2 * precision["rate"] * recall["rate"] / (precision["rate"] + recall["rate"])

    def wrong_row(r: dict[str, Any]) -> bool:
        return not r["name_ok"] or r["price_check"] in ("wrong", "fabricated", "missing")

    low = [r for r in rows if r["confidence"] is not None and r["confidence"] < LOW_CONFIDENCE]
    high = [r for r in rows if r["confidence"] is not None and r["confidence"] >= LOW_CONFIDENCE]
    return {
        "pages": len(pages), "products": extracted, "missed": missed,
        "name_precision": precision, "name_recall": recall, "name_f1": f1,
        "missing_product_rate": rate(missed, right + missed),
        "price_accuracy": rate(count["correct"], truth_priced),
        "price_wrong": rate(count["wrong"], truth_priced),
        "price_missed": rate(count["missing"], truth_priced),
        "price_fabrication": rate(count["fabricated"], given),
        "field_correction": rate((extracted - right) + (count["wrong"] + count["fabricated"] + count["missing"]),
                                 extracted + len(checks)),
        "error_rate_low_confidence": rate(sum(map(wrong_row, low)), len(low)),
        "error_rate_high_confidence": rate(sum(map(wrong_row, high)), len(high)),
    }


# Report

def build(repo: Repo, catalogs: list[str] | None, audit_path: Path | None, project_pages: int | None,
          manual_min: float | None, assisted_min: float | None) -> dict[str, Any]:
    data = measured(repo, catalogs)
    t = data["totals"]
    ok = t.get("ok", 0)
    products = t.get("products", 0)
    walls = data["batch_walls_s"]
    result: dict[str, Any] = {"model": repo.model, "measured": data, "reviewed": reviewed(repo, catalogs)}
    cost, wasted = t.get("cost", 0), t.get("wasted_cost", 0)
    result["cost"] = {
        "total_usd": cost, "per_page_usd": cost / ok if ok else None,
        "per_product_usd": cost / products if products else None,
        "wasted_usd": wasted, "wasted_share": wasted / cost if cost else None,
        "projected_usd": cost / ok * project_pages if ok and project_pages else None,
        "projected_pages": project_pages}
    result["latency"] = {
        "batches": len(walls), "p50_s": percentile(walls, .5) if walls else None,
        "p95_s": percentile(walls, .95) if walls else None, "min_s": min(walls) if walls else None,
        "max_s": max(walls) if walls else None,
        "pages_per_hour_median": percentile(data["batch_pages_per_hour"], .5) if data["batch_pages_per_hour"] else None}
    resolved = ok + t.get("error", 0)
    unresolved = sum(data["submission_states"].get(s, 0) for s in ("prepared", "submitting", "unknown", "needs_review"))
    total_subs = sum(data["submission_states"].values())
    result["reliability"] = {
        "success": rate(int(ok), int(resolved)), "retried_pages": rate(int(t.get("pages_retried", 0)), int(t.get("pages_tried", 0))),
        "failed_attempts": rate(int(t.get("failed_attempts", 0)), int(t.get("attempts", 0))),
        "unresolved_submissions": rate(unresolved, total_subs),
        "released_submissions": rate(data["submission_states"].get("released", 0), total_subs)}
    result["signals"] = {
        "products_per_product_page": products / t["product_pages"] if t.get("product_pages") else None,
        "non_product_share": rate(int(t.get("non_product_pages", 0)), int(ok)),
        "price_not_found": rate(int(t.get("no_price", 0)), int(products)),
        "low_confidence": rate(int(t.get("low_confidence", 0)), int(products))}
    result["audit"] = None
    if audit_path:
        pages, problems = load_audit(audit_path)
        result["audit"] = {**audited(pages), "problems": problems}
        precision = result["audit"]["name_precision"]["rate"]
        result["cost"]["per_correct_product_usd"] = (result["cost"]["per_product_usd"] / precision
                                                     if precision and result["cost"]["per_product_usd"] else None)
    if manual_min is not None and assisted_min is not None and products:
        saved = (manual_min - assisted_min) * products / 60
        result["time_saved"] = {"products": int(products), "manual_hours": manual_min * products / 60,
                                "assisted_hours": assisted_min * products / 60, "hours_saved": saved,
                                "reduction": 1 - assisted_min / manual_min if manual_min else None}
    return result


def _pct(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a (no data)"
    text = f"{r['rate']:.1%}  ({r['k']}/{r['n']}"
    if r["ci_low"] is not None:
        text += f", 95% CI {r['ci_low']:.1%}-{r['ci_high']:.1%}"
    return text + ")"


def _usd(v: float | None, digits: int = 4) -> str:
    return "n/a" if v is None else f"${v:,.{digits}f}"


def _secs(v: float | None) -> str:
    if v is None:
        return "n/a"
    m, s = divmod(int(round(v)), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def render(r: dict[str, Any]) -> str:
    t, c, lat, rel, sig = r["measured"]["totals"], r["cost"], r["latency"], r["reliability"], r["signals"]
    out = [f"PRODUCTION BENCHMARKS  (model {r['model']})", "", "VOLUME  [measured]"]
    for name, row in r["measured"]["catalogs"].items():
        out.append(f"  {name:<28} ok {int(row.get('ok', 0)):>4}  error {int(row.get('error', 0)):>3}  "
                   f"in flight {int(row.get('in_flight', 0)):>3}  products {int(row.get('products', 0)):>5}  "
                   f"cost {_usd(row.get('cost', 0))}")
    out += [f"  {'TOTAL':<28} ok {int(t.get('ok', 0)):>4}  error {int(t.get('error', 0)):>3}  "
            f"in flight {int(t.get('in_flight', 0)):>3}  products {int(t.get('products', 0)):>5}  "
            f"cost {_usd(t.get('cost', 0))}", "", "COST  [measured]",
            f"  per page (all-in)     {_usd(c['per_page_usd'], 5)}", f"  per product           {_usd(c['per_product_usd'], 5)}"]
    if "per_correct_product_usd" in c:
        out.append(f"  per correct product   {_usd(c['per_correct_product_usd'], 5)}   (per product / name precision)")
    out.append(f"  wasted on failed tries {_usd(c['wasted_usd'], 5)}"
               + (f"  ({c['wasted_share']:.1%} of spend)" if c["wasted_share"] is not None else ""))
    if c["projected_usd"] is not None:
        out.append(f"  projected for {c['projected_pages']:,} pages  {_usd(c['projected_usd'], 2)}   "
                   "(linear; depends on how many are product pages)")
    out += ["", "LATENCY AND THROUGHPUT  [measured]  (Anthropic batch created -> ended)",
            f"  batches measured      {lat['batches']}"]
    if lat["batches"]:
        out += [f"  turnaround p50 / p95  {_secs(lat['p50_s'])} / {_secs(lat['p95_s'])}",
                f"  turnaround min / max  {_secs(lat['min_s'])} / {_secs(lat['max_s'])}",
                f"  pages per hour        {lat['pages_per_hour_median']:.0f}  (median batch)"]
        if lat["batches"] < 20:
            out.append(f"  note: only {lat['batches']} batch(es); read min/max, not p95. Submit at different times of day.")
    out += ["", "RELIABILITY  [measured]", f"  page success          {_pct(rel['success'])}",
            f"  pages retried         {_pct(rel['retried_pages'])}", f"  failed attempts       {_pct(rel['failed_attempts'])}",
            f"  unresolved now        {_pct(rel['unresolved_submissions'])}  of submissions",
            f"  released              {_pct(rel['released_submissions'])}  of submissions",
            "", "EXTRACTION SIGNALS  [measured; not accuracy]",
            "  products per product page  "
            + ("n/a" if sig["products_per_product_page"] is None else f"{sig['products_per_product_page']:.1f}"),
            f"  non-product pages     {_pct(sig['non_product_share'])}",
            f"  price not found       {_pct(sig['price_not_found'])}",
            f"  low confidence (<{LOW_CONFIDENCE})  {_pct(sig['low_confidence'])}", "", "HUMAN CORRECTION  [reviewed catalogues only]"]
    rv = r["reviewed"]
    if not rv["pages"]:
        out.append("  n/a: no catalogue has completed level-1 review yet")
    else:
        out += [f"  pages reviewed        {rv['pages']}  ({rv['raw_products']} raw products)",
                f"  field correction      {_pct(rv['field_correction'])}", f"  product correction    {_pct(rv['product_correction'])}",
                f"  deleted / added rows  {rv['deleted']} / {rv['added']}",
                "  fields changed        " + ", ".join(f"{k} {v}" for k, v in rv["by_field"].items() if v)]
    out += ["", "ACCURACY  [audited: human-checked sample]"]
    a = r["audit"]
    if a is None:
        out.append("  n/a: no audit sheet. Run --export-audit, fill it in, then rerun with --audit.")
    else:
        out += [f"  audited               {a['pages']} pages, {a['products']} extracted products, {a['missed']} missed",
                f"  name precision        {_pct(a['name_precision'])}", f"  name recall           {_pct(a['name_recall'])}",
                f"  name F1               {'n/a' if a['name_f1'] is None else format(a['name_f1'], '.1%')}",
                f"  missing-product rate  {_pct(a['missing_product_rate'])}",
                f"  price accuracy        {_pct(a['price_accuracy'])}   of products whose page shows a price",
                f"    wrong price         {_pct(a['price_wrong'])}", f"    price not extracted {_pct(a['price_missed'])}",
                f"  price fabrication     {_pct(a['price_fabrication'])}   of extracted prices with none on the page",
                f"  field correction      {_pct(a['field_correction'])}   (name and price fields)",
                f"  error, confidence <{LOW_CONFIDENCE}   {_pct(a['error_rate_low_confidence'])}",
                f"  error, confidence >={LOW_CONFIDENCE}  {_pct(a['error_rate_high_confidence'])}"]
        out += [f"  PROBLEM: {p}" for p in a["problems"][:20]]
    out += ["", "BUSINESS IMPACT"]
    ts = r.get("time_saved")
    if ts:
        out += [f"  manual {ts['manual_hours']:.1f} h vs assisted {ts['assisted_hours']:.1f} h for {ts['products']} products "
                f"-> {ts['hours_saved']:.1f} h saved ({ts['reduction']:.0%} less)", "  (from the minutes-per-product you supplied)"]
    else:
        out.append("  n/a: pass --manual-min-per-product and --assisted-min-per-product (time them with a stopwatch)")
    return "\n".join(out)


def run(repo: Repo, args) -> int:
    catalogs = args.catalogs or None
    if args.export_audit:
        pages, rows = export_audit(repo, Path(args.export_audit), args.audit_size, args.seed, catalogs)
        print(f"wrote {args.export_audit}: {pages} pages, {rows} products.\n"
              "Fill name_correct (y/n), price_check (correct/wrong/fabricated/missing/none) per product, and "
              "missed_on_page (a number, 0 if none) on the first row of every page. Then rerun with --audit.")
        return 0
    report = build(repo, catalogs, Path(args.audit) if args.audit else None, args.project_pages,
                   args.manual_min_per_product, args.assisted_min_per_product)
    print(render(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    return 0
