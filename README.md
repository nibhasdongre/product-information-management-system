# Product Information Management: catalogue extraction

Turns vendor catalogue PDFs into structured product records. Claude Sonnet 5 reads each page through the Batch API (half price); results are stored in Supabase Postgres, ready for review and the storefront.

Status: the extraction pipeline runs on the database. The review UI, storefront, hourly worker and migration of older filesystem results are not built yet.

## Layout

```
.env                        ANTHROPIC_API_KEY, DATABASE_URL (not in git)
requirements.txt            runtime dependencies
requirements-dev.txt        adds pytest
supabase/migrations/        schema, applied in filename order
extraction/
  config/                   settings.yaml, models.yaml
  pipeline/                 the package (run with python -m pipeline)
  tests/                    pytest suite; real_*.py scripts hit the live API
```

## Setup

Use Python 3.11 or later. From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

Create a Supabase project, then create `.env` in the repository root:

```
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=postgresql://postgres.PROJECTREF:PASSWORD@HOST:5432/postgres
```

- Use the **Session pooler** string (Project Settings, Database, Connect), on port 5432. The direct host is IPv6-only, and the transaction pooler (port 6543) breaks the batch lock.
- URL-encode special characters in the password.

Create the tables, from `extraction/`:

```powershell
python -m pipeline db migrate
python -m pipeline db check
```

`db migrate` refuses to run if the tables already exist. `db check` prints the server version and confirms the 12 tables exist with row-level security on. RLS is enabled on every table with no policies, so the public Supabase API returns nothing until policies are added.

## Configuration

| Setting | Where | Default |
|---|---|---|
| Catalogue folder | `PIM_CATALOG_DIR`, or `catalog_dir` in `config/settings.yaml` | `extraction/data/catalogs` |
| Image folder | `PIM_OUTPUT_DIR`, or `output_dir` in `config/settings.yaml` | `extraction/data/runs` |
| Render resolution | `render_dpi` in `config/settings.yaml` | 150 |
| Batch size limits | `batch` in `config/settings.yaml` | 10,000 requests, 200 MB |
| Model and prices | `config/models.yaml` | `claude-sonnet-5`, batch discount 0.5 |

Each catalogue is a PDF named after its brand: `Jack_Jones.pdf` is catalogue `Jack_Jones` and brand "Jack Jones". The catalogue and brand are registered automatically at the first `batch submit`. A wrong brand name can be fixed at review. A re-uploaded PDF is recognised by its SHA-256, whatever it is called.

## Commands

Run from `extraction/`. Replace `Turtle` with a catalogue key. Batch is the only extraction mode; the old synchronous `run` command was removed.

| Command | What it does |
|---|---|
| `python -m pipeline list` | Lists PDFs in the catalogue folder with page counts. |
| `python -m pipeline report [catalogues] [--detail]` | Pages, cost and tokens per catalogue, plus a batch table. Reads the database; no API calls. |
| `python -m pipeline batch submit [catalogues] [--pages 1,3-5]` | Sends pending pages as batches at half price. |
| `python -m pipeline batch status` | Reconciles unresolved submissions, then shows batch progress. |
| `python -m pipeline batch collect` | Writes results for ended batches. |
| `python -m pipeline batch sync` | Reconcile, status and collect in one run. Suitable for a schedule. |
| `python -m pipeline batch resolve ID --adopt BATCH` | Records a batch for a submission that needs review, if its results match. |
| `python -m pipeline batch resolve ID --release --confirm-no-batch` | Frees a submission's pages after confirming no batch exists. |
| `python -m pipeline db check` / `db migrate` | See Setup. |

Batch commands accept `--model`. Exit codes for `batch` commands: `0` success, `1` failed pages or invalid resolve arguments, `2` invalid API key, `3` unresolved or unknown submission, `4` another batch command holds the lock.

## How a batch flows

1. **Submit.** Each pending page is rendered and, in one transaction, reserved together with its submission row. The create call follows. Once Anthropic accepts the batch, its id, the `submitted` state and the pages' status are recorded in one transaction.
2. **Collect.** For each ended batch, every page is written in one statement: status, token and cost attempt row, raw model output, and product rows. The page image is re-rendered from the PDF at that point, so no images are kept between submit and collect. Collect can run on a different machine from submit, as long as the PDF is at the path stored in `catalog.storage_path`.
3. **Retry.** A page that errors or returns no result is sent again on the next submit. Earlier attempts stay in `extraction_attempts`, so their cost is kept.

`extraction_data` is append-only: a trigger blocks updates. Reviewers edit `product`, never the raw output.

## Batch recovery

Every batch command first reconciles unresolved submissions:

- A submission is recorded only after its batch's results carry its own custom IDs (`<submission prefix>-<extraction_id>`). Matching on time and request count alone is never enough.
- A submission that cannot be resolved is reported as `NEEDS REVIEW` after 20 hours; the batch expires 24 hours after creation.
- While any submission is unresolved, `batch submit` refuses to run.
- A page that belongs to an unresolved or recorded submission is never sent again.
- A create call is never retried after an unclear failure, so a duplicate batch cannot be created. Only a received 429 is retried.
- One batch command runs at a time, enforced by a Postgres advisory lock.

Use one database per API key. Candidate batches are matched by time and request count, so two databases on the same key can interfere with each other.

## Where results go

| Data | Location |
|---|---|
| Raw model output per page | `extraction_data.raw_output` |
| Reviewer-editable products | `product` (one row per variant) |
| Tokens, cost, render time per attempt | `extraction_attempts` |
| Submission and batch state | `batch_submissions` |
| Page images | `<output>/<model>/<catalogue>/images/`, path in `extraction_data.image_path` |

Weights such as "247 gms" are parsed into a number and unit only when clearly valid; otherwise they are left empty and remain in the raw output. A product's subcategory is set only when the page prints both a category and a subcategory.

## Tests

From the repository root:

```powershell
python -m pytest extraction/tests -q
```

Tests that need the database are skipped when `DATABASE_URL` is unset. They run in a throwaway schema that is dropped afterwards, so real data is never touched. The suite uses a fake Batch API and costs no API money, but each database statement is a network round trip, so a full run takes several minutes against a remote project.

Two real-API scripts are run by hand, and each creates a real batch that writes to the database named in `.env`:

- `python tests/real_crash_child.py Turtle --pages 1-3`: exits after a batch is created, to exercise recovery.
- `python tests/real_release_child.py Turtle --pages 1-3`: exits before the create call, to exercise release.

## Known limits

- Tested on Windows only.
- A live 429 response and a batch's 24-hour expiry are covered by unit tests only.
- The database path has not yet been run end to end against the real Batch API.
- `catalog.storage_path` is a local file path for now. A scheduled worker needs the PDFs and page images on shared storage, which is not decided yet.
- Results from the earlier filesystem version (`results/`) have not been migrated into the database.
- Page type (product or not) is decided by the model. Summary and introduction pages can be misclassified; reviewers check every page.
