"""Command-line entry point: python -m pipeline {list,report,benchmark,batch,db,storage} ..."""
from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from pipeline import batch, benchmark, db, submission
from pipeline.config import PACKAGE_ROOT, get_model, load_models, load_settings
from pipeline.db import LockHeld
from pipeline.pdf import catalog_path, discover, open_catalog
from pipeline.providers import build_provider
from pipeline.providers.base import ProviderAuthError
from pipeline.report import catalog_rows, format_batches, format_catalogs, format_pages
from pipeline.repo import Repo
from pipeline.storage import StorageError, build_storage


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

    db_cmd = sub.add_parser("db", help="Database: check the connection and schema, apply migrations.")
    db_sub = db_cmd.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("check", help="Print server version; confirm the 12 tables exist with RLS on.")
    db_sub.add_parser("migrate", help="Apply supabase/migrations/*.sql (only into an empty schema).")

    storage_cmd = sub.add_parser("storage", help="Object storage for PDFs and page images.")
    storage_sub = storage_cmd.add_subparsers(dest="storage_command", required=True)
    storage_sub.add_parser("check", help="Show which backend is used; write, read back and delete a probe object.")

    report = sub.add_parser("report", help="Print cost and tokens per catalog and batch; no API calls.")
    report.add_argument("catalogs", nargs="*", help="Catalog keys. Default: all registered catalogs.")
    report.add_argument("--model", help="Model name whose results to report. Default: settings.default_model.")
    report.add_argument("--detail", action="store_true", help="Also print a row per page.")

    bench = sub.add_parser("benchmark", help="Cost, latency, reliability and (with an audit sheet) accuracy.")
    bench.add_argument("catalogs", nargs="*", help="Catalog keys. Default: all registered catalogs.")
    bench.add_argument("--model", help="Model name. Default: settings.default_model.")
    bench.add_argument("--export-audit", metavar="CSV", help="Write a sample audit sheet to fill in, then exit.")
    bench.add_argument("--audit-size", type=int, default=100, help="Pages in the audit sample. Default: 100.")
    bench.add_argument("--seed", type=int, default=1, help="Sampling seed, so a sample can be reproduced.")
    bench.add_argument("--audit", metavar="CSV", help="Filled-in audit sheet: adds accuracy and fabrication.")
    bench.add_argument("--project-pages", type=int, help="Project the cost for this many pages.")
    bench.add_argument("--manual-min-per-product", type=float, help="Measured manual entry time per product.")
    bench.add_argument("--assisted-min-per-product", type=float, help="Measured review time per product.")
    bench.add_argument("--json", metavar="PATH", help="Also write the numbers as JSON.")

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


def _print_report(repo: Repo, catalogs: list[str] | None, detail: bool, catalog_dir) -> None:
    rows = catalog_rows(repo.catalog_stats(catalogs or None), catalog_dir)
    if detail:
        for row in rows:
            print(f"\n{row['catalog_name']}")
            print(format_pages(repo.page_rows(row["catalog_id"])))
    print()
    print(format_catalogs(rows))
    batches = repo.all_batches()
    if batches:
        print()
        print(format_batches(batches))


def _run_batch(args, settings) -> int:
    model = get_model(load_models(), args.model or settings.default_model)
    provider = build_provider(model, settings.retry)
    storage = build_storage(settings.output_dir)
    logging.getLogger(__name__).info("storage: %s", storage.describe())

    with db.advisory_lock(), db.connect(autocommit=True) as conn:
        repo = Repo(conn, model.name)
        for line in submission.reconcile(provider, repo):
            print(line)

        if args.batch_command == "submit":
            catalogs = args.catalogs or discover(settings.catalog_dir)
            for key in catalogs:
                catalog_path(settings.catalog_dir, key)
            report = submission.submit(provider=provider, model=model, repo=repo, settings=settings,
                                       storage=storage, catalog_dir=settings.catalog_dir,
                                       catalogs=catalogs, pages=args.pages)
            print(f"submitted {report.submitted} page(s) in {len(report.batch_ids)} batch(es); "
                  f"skipped {report.skipped}; failed to render {report.failed}")
            for batch_id in report.batch_ids:
                print(f"  {batch_id}")
            return 1 if report.failed else 0

        if args.batch_command == "status":
            lines = batch.refresh_status(provider, repo)
            print("\n".join(lines) if lines else "no batches awaiting collection")
            return 0

        if args.batch_command == "resolve":
            if args.adopt:
                print(submission.resolve_adopt(provider, repo, args.submission_id, args.adopt))
                return 0
            if args.release:
                if not args.confirm_no_batch:
                    print("refusing to release without --confirm-no-batch", file=sys.stderr)
                    return 1
                print(submission.resolve_release(repo, args.submission_id))
                return 0
            print("specify --adopt BATCH_ID or --release --confirm-no-batch", file=sys.stderr)
            return 1

        if args.batch_command == "sync":
            for line in batch.refresh_status(provider, repo):
                print(line)
        collected = batch.collect(provider, repo, model)
        print(f"collected {len(collected)} batch(es)" + (f": {', '.join(collected)}" if collected else ""))
        if collected:
            _print_report(repo, None, False, settings.catalog_dir)
        return 0


def _run_storage(settings) -> int:
    try:
        storage = build_storage(settings.output_dir)
        print(f"backend: {storage.describe()}")
        storage.check()
    except StorageError as exc:
        print(f"storage check failed: {exc}", file=sys.stderr)
        return 5
    print("ok: wrote, read back and deleted a probe object")
    return 0


def _run_db(args) -> int:
    with db.connect() as conn:
        if args.db_command == "migrate":
            existing = [t for t in db.EXPECTED_TABLES if t not in db.check(conn)["missing"]]
            if existing:
                print(f"Refusing to migrate: tables already exist: {', '.join(existing)}", file=sys.stderr)
                return 1
            for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
                db.apply_migration(conn, path)
                print(f"applied {path.name}")
        result = db.check(conn)
    print(result["version"])
    if result["missing"]:
        print("MISSING tables:", ", ".join(result["missing"]))
    if result["no_rls"]:
        print("RLS OFF on:", ", ".join(result["no_rls"]))
    ok = not result["missing"] and not result["no_rls"]
    print(f"{len(db.EXPECTED_TABLES) - len(result['missing'])}/{len(db.EXPECTED_TABLES)} tables, "
          f"RLS {'on for all' if ok else 'INCOMPLETE'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env_file = PACKAGE_ROOT.parent / ".env"
    load_dotenv(env_file, override=True)
    logging.getLogger(__name__).info("env file %s exists=%s; ANTHROPIC_API_KEY set=%s",
                                     env_file, env_file.exists(), bool(os.environ.get("ANTHROPIC_API_KEY")))

    if args.command == "db":
        return _run_db(args)

    settings = load_settings()

    if args.command == "list":
        for key in discover(settings.catalog_dir):
            with open_catalog(settings.catalog_dir, key) as doc:
                print(f"{key}\t{doc.page_count} pages")
        return 0

    if args.command == "storage":
        return _run_storage(settings)

    if args.command == "report":
        model = get_model(load_models(), args.model or settings.default_model)
        with db.connect(autocommit=True) as conn:
            _print_report(Repo(conn, model.name), args.catalogs, args.detail, settings.catalog_dir)
        return 0

    if args.command == "benchmark":
        model = get_model(load_models(), args.model or settings.default_model)
        with db.connect(autocommit=True) as conn:
            return benchmark.run(Repo(conn, model.name), args)

    try:
        return _run_batch(args, settings)
    except LockHeld as exc:
        print(f"{exc}. Wait for it to finish.", file=sys.stderr)
        return 4
    except submission.UnresolvedSubmissions as exc:
        print(f"{exc}. Run `batch status` or `batch resolve`.", file=sys.stderr)
        return 3
    except submission.SubmissionUnknown as exc:
        print(f"create outcome unknown for submission {exc}. Run `batch status` to reconcile.", file=sys.stderr)
        return 3
    except ProviderAuthError as exc:
        print(f"Stopped: {exc}. Check ANTHROPIC_API_KEY in the project-root .env.", file=sys.stderr)
        return 2
    except StorageError as exc:
        print(f"Stopped: storage failed: {exc}. Run `python -m pipeline storage check`.", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
