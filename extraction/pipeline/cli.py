"""Command-line entry point: python -m pipeline {list,run,report,batch} ..."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from pipeline import batch, submission
from pipeline.config import PACKAGE_ROOT, get_model, load_models, load_settings
from pipeline.durable import LockHeld
from pipeline.extract import extract_catalog
from pipeline.pdf import catalog_path, discover, open_catalog
from pipeline.providers import build_provider
from pipeline.providers.base import ProviderAuthError
from pipeline.report import format_batches, format_catalogs, format_pages, summarize
from pipeline.store import OutputStore


def _parse_pages(spec: str) -> list[int]:
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = (int(value) for value in part.split("-", 1))
                if start < 1 or end < start:
                    raise ValueError(part)
                pages.update(range(start, end + 1))
            else:
                page = int(part)
                if page < 1:
                    raise ValueError(part)
                pages.add(page)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid page selection {part!r}; use e.g. 1,3-5") from None
    if not pages:
        raise argparse.ArgumentTypeError("no pages selected; use e.g. 1,3-5")
    return sorted(pages)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline", description="Extract products from catalog PDFs.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List catalogs found in the catalog directory.")

    run = sub.add_parser("run", help="Extract synchronously, then print the cost report.")
    run.add_argument("catalogs", nargs="*", help="Catalog keys (PDF file stems). Default: all.")
    run.add_argument("--model", help="Model name from config/models.yaml. Default: settings.default_model.")
    run.add_argument("--pages", type=_parse_pages, help="Page selection, e.g. 1,3-5. Default: all pages.")
    run.add_argument("--dry-run", action="store_true", help="Validate inputs and count pages; no API calls.")

    report = sub.add_parser("report", help="Print cost and latency from saved page files; no API calls.")
    report.add_argument("catalogs", nargs="*", help="Catalog keys. Default: all.")
    report.add_argument("--model", help="Model name whose results to report. Default: settings.default_model.")
    report.add_argument("--detail", action="store_true", help="Also print a row per page.")

    batch_cmd = sub.add_parser("batch", help="Batch API at half price: submit, status, collect.")
    batch_sub = batch_cmd.add_subparsers(dest="batch_command", required=True)
    b_submit = batch_sub.add_parser("submit", help="Render pending pages and submit them as batches.")
    b_submit.add_argument("catalogs", nargs="*", help="Catalog keys. Default: all.")
    b_submit.add_argument("--model", help="Model name. Default: settings.default_model.")
    b_submit.add_argument("--pages", type=_parse_pages, help="Page selection, e.g. 1,3-5. Default: all pages.")
    b_status = batch_sub.add_parser("status", help="Poll batches that are not yet collected.")
    b_status.add_argument("--model", help="Model name. Default: settings.default_model.")
    b_collect = batch_sub.add_parser("collect", help="Write results for batches that have ended.")
    b_collect.add_argument("--model", help="Model name. Default: settings.default_model.")
    b_sync = batch_sub.add_parser("sync", help="Reconcile, refresh status, and collect in one run.")
    b_sync.add_argument("--model", help="Model name. Default: settings.default_model.")
    b_resolve = batch_sub.add_parser("resolve", help="Decide an unresolved submission that needs review.")
    b_resolve.add_argument("submission_id")
    b_resolve.add_argument("--model", help="Model name. Default: settings.default_model.")
    b_resolve.add_argument("--adopt", metavar="BATCH_ID",
                           help="Record BATCH_ID as this submission's batch, if its results verify.")
    b_resolve.add_argument("--release", action="store_true",
                           help="Release the submission so its pages can be sent again.")
    b_resolve.add_argument("--confirm-no-batch", action="store_true",
                           help="Required with --release: confirm in the Anthropic console that no batch exists.")
    return parser


def _print_report(store: OutputStore, catalog_dir: Path, catalogs: list[str], detail: bool) -> None:
    summaries = []
    for key in catalogs:
        summary, records = summarize(store, catalog_dir, key)
        store.save_summary(key, asdict(summary))
        summaries.append(summary)
        if detail:
            print(f"\n{key}")
            print(format_pages(records))
    print()
    print(format_catalogs(summaries))
    manifests = store.list_manifests()
    if manifests:
        print()
        print(format_batches(manifests))


def _run_batch(args, settings) -> int:
    model = get_model(load_models(), args.model or settings.default_model)
    store = OutputStore(settings.output_dir, model.name)
    provider = build_provider(model, settings.retry)

    with store.lock():
        for line in submission.reconcile(provider, store):
            print(line)

        if args.batch_command == "submit":
            catalogs = args.catalogs or discover(settings.catalog_dir)
            for key in catalogs:
                catalog_path(settings.catalog_dir, key)
            report = submission.submit(provider=provider, model=model, store=store, settings=settings,
                                       catalog_dir=settings.catalog_dir, catalogs=catalogs,
                                       pages=args.pages)
            print(f"submitted {report.submitted} page(s) in {len(report.batch_ids)} batch(es); "
                  f"skipped {report.skipped}; failed to render {report.failed}")
            for batch_id in report.batch_ids:
                print(f"  {batch_id}")
            return 1 if report.failed else 0

        if args.batch_command == "status":
            lines = batch.refresh_status(provider, store)
            print("\n".join(lines) if lines else "no batches awaiting collection")
            return 0

        if args.batch_command == "sync":
            for line in batch.refresh_status(provider, store):
                print(line)
            collected = batch.collect(provider, store, model)
            print(f"collected {len(collected)} batch(es)" + (f": {', '.join(collected)}" if collected else ""))
            _print_report(store, settings.catalog_dir, discover(settings.catalog_dir), detail=False)
            return 0

        if args.batch_command == "resolve":
            if args.adopt:
                print(submission.resolve_adopt(provider, store, args.submission_id, args.adopt))
                return 0
            if args.release:
                if not args.confirm_no_batch:
                    print("refusing to release without --confirm-no-batch", file=sys.stderr)
                    return 1
                print(submission.resolve_release(store, args.submission_id))
                return 0
            print("specify --adopt BATCH_ID or --release --confirm-no-batch", file=sys.stderr)
            return 1

        collected = batch.collect(provider, store, model)
        print(f"collected {len(collected)} batch(es)" + (f": {', '.join(collected)}" if collected else ""))
        if collected:
            _print_report(store, settings.catalog_dir, discover(settings.catalog_dir), detail=False)
        return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env_file = PACKAGE_ROOT.parent / ".env"
    load_dotenv(env_file, override=True)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    logging.getLogger(__name__).info("env file %s exists=%s; ANTHROPIC_API_KEY set=%s prefix=%s",
                                     env_file, env_file.exists(), bool(key), key[:7])
    settings = load_settings()

    if args.command == "list":
        for key in discover(settings.catalog_dir):
            with open_catalog(settings.catalog_dir, key) as doc:
                print(f"{key}\t{doc.page_count} pages")
        return 0

    if args.command == "report":
        store = OutputStore(settings.output_dir, args.model or settings.default_model)
        catalogs = args.catalogs or discover(settings.catalog_dir)
        _print_report(store, settings.catalog_dir, catalogs, args.detail)
        return 0

    if args.command == "batch":
        try:
            return _run_batch(args, settings)
        except LockHeld as exc:
            print(f"{exc}. Wait for it to finish.", file=sys.stderr)
            return 4
        except submission.UnresolvedSubmissions as exc:
            print(f"{exc}. Run `batch status` or `batch resolve`.", file=sys.stderr)
            return 3
        except submission.SubmissionUnknown as exc:
            print(f"create outcome unknown for submission {exc}. Run `batch status` to reconcile.",
                  file=sys.stderr)
            return 3
        except ProviderAuthError as exc:
            print(f"Stopped: {exc}. Check ANTHROPIC_API_KEY in pim/.env.", file=sys.stderr)
            return 2

    models = load_models()
    model = get_model(models, args.model or settings.default_model)
    catalogs = args.catalogs or discover(settings.catalog_dir)
    for key in catalogs:
        catalog_path(settings.catalog_dir, key)  # fail fast before any API call
    pages = args.pages

    if args.dry_run:
        for key in catalogs:
            with open_catalog(settings.catalog_dir, key) as doc:
                count = len(pages) if pages else doc.page_count
            print(f"{key}\t{count} page(s) would run on {model.name}")
        return 0

    provider = build_provider(model, settings.retry)
    store = OutputStore(settings.output_dir, model.name)
    total_failed = 0
    try:
        for key in catalogs:
            report = extract_catalog(
                catalog=key, catalog_dir=settings.catalog_dir, provider=provider, model=model,
                store=store, dpi=settings.render_dpi, pages=pages,
            )
            total_failed += report.failed
    except ProviderAuthError as exc:
        print(f"Stopped: {exc}. Check ANTHROPIC_API_KEY in pim/.env.", file=sys.stderr)
        return 2
    _print_report(store, settings.catalog_dir, catalogs, detail=False)
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
