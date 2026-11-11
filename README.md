# Product Information Management: catalogue extraction

Turns vendor catalogue PDFs into structured product records. Claude Sonnet 5 reads each page through the Batch API (half price); results are stored in Supabase Postgres, and the catalogue PDFs in Supabase Storage, ready for review and the storefront.

Status: the extraction pipeline runs on the database and object storage. The review backend (database functions and rules) and the image publisher are built and tested; the review UI, storefront and hourly worker are not built yet.

## Layout

```
.env                        ANTHROPIC_API_KEY, DATABASE_URL, Supabase Storage keys (not in git)
requirements.txt            runtime dependencies
requirements-dev.txt        adds pytest
supabase/migrations/        schema, applied in filename order
.github/workflows/         test job (GitHub Actions)
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
SUPABASE_URL=https://PROJECTREF.supabase.co
SUPABASE_SECRET_KEY=your-secret-key
```

- Use the **Session pooler** string (Project Settings, Database, Connect), on port 5432. The direct host is IPv6-only, and the transaction pooler (port 6543) breaks the batch lock.
- URL-encode special characters in the password.

Create the tables, from `extraction/`:

```powershell
python -m pipeline db migrate
python -m pipeline db check
```

`db migrate` applies every migration in `supabase/migrations/` that has not been applied yet, in order, and records each one in `supabase_migrations.schema_migrations` (the table the Supabase CLI uses, so `supabase db push` can take over later). Each migration and its record are written in one transaction, so a failing migration changes nothing and is retried next time. A database built before tracking existed is recognised and baselined without being rebuilt. Add a schema change as a new file named `<14-digit timestamp>_<name>.sql`; never edit one that has been applied. `db check` prints the server version and confirms the 12 tables exist with row-level security on. RLS is enabled on every table with no policies, so the public Supabase API returns nothing until policies are added.

## Review backend

The review screen talks to Postgres only through functions (`supabase/migrations/20261111000000_review_stage.sql`). They run with the table owner's rights and check who is calling, so the rules hold whatever the screen does. The tables stay locked: a browser cannot read or write them directly. Call them from supabase-js with `supabase.rpc('rv_open', {cid: 1})`; parameter names are in the SQL.

How a catalogue is reviewed:
- **Ready** when every page has an extraction. A page whose extraction failed is shown as a non-product page, and the reviewer can add products to it.
- **Two levels**, each a full pass by a different person. `rv_open` claims the next level for the caller or resumes their own. Only one person holds a level at a time, and the Level 1 person cannot do Level 2.
- **Drafts live on the server.** `rv_save_product`, `rv_add_product`, `rv_remove_product`, `rv_restore_product` and `rv_set_brand` change only the draft. `rv_get_page` returns a page with the draft laid over it and records that the page was viewed.
- **`rv_finish`** needs every page viewed and every product to have a name, a price and a currency. It then applies all edits in one transaction, or none, and writes the audit trail. `rv_release` gives a level up and discards its draft; an admin can release anyone's.
- **Crop** is saved in `product.crop` as fractions of the page: null is the whole page, `{"none": true}` is no image, `{x, y, w, h}` is a rectangle. A changed crop clears `cropped_path`, which is the publisher's cue to cut the image again.
- **Lists:** `rv_lists` returns brands, categories, subcategories and tags. Any signed-in user can add categories and subcategories (`rv_add_category`, `rv_add_subcategory`); tags are created when a product using them is saved at Finish.
- **Audit:** `review_log` records every applied change with before and after values, plus claims, releases, finishes and changes to users and lists. It cannot be updated or deleted. Admins read it with `rv_audit`.

Users and roles (`reviewer`, `admin`): sign-up is invite-only. Turn off public sign-ups in the Supabase dashboard (Authentication, Providers, Email), invite each person under Authentication, Users, then an admin calls `admin_add_user(email, role)`. A signed-in account with no role can do nothing. The first admin has to be inserted by hand from the SQL editor: `insert into app_user (user_id, email, role) select id, email, 'admin' from auth.users where email = 'you@example.com';`.

Reviewers read the catalogue PDF straight from the private bucket: the migration adds a read policy on `storage.objects` for active users. A page tells the viewer's PDF reader the file's `storage_path` from `rv_open`.

## Storage

Catalogue PDFs go to a **private** Supabase Storage bucket named `catalogs` (override with `SUPABASE_STORAGE_BUCKET`):

| Object | Key | Notes |
|---|---|---|
| Catalogue PDF | `pdfs/<sha256>.pdf` | uploaded when a catalogue is first registered; the key is stored in `catalog.storage_path` |

- `SUPABASE_URL` is the Project URL, and `SUPABASE_SECRET_KEY` is a secret (service-role) key from Project Settings, API. It bypasses all access rules, so it belongs only in `.env` and the worker's secrets, never in a browser or the web app.
- Set both or neither. With neither, files go to a local folder (`<output folder>/storage`), which is how the tests run. The first line of every batch command logs which backend is in use.
- Create the bucket and verify the keys with `python -m pipeline storage check`. It makes the bucket if it is missing, then writes, reads back and deletes a probe object.
- Keep the bucket private: catalogue PDFs are supplier documents and can show internal data, such as stock tables. Reviewers will get short-lived signed links; the storefront will serve only published product crops, from a separate public bucket.

## Publishing images

`python -m pipeline publish` turns reviewed crops into storefront images. A product is picked up once both review levels of its catalogue are done and it has no published image. Its crop (a rectangle as fractions of the page, or the whole page when unset) is cut from the PDF and saved as two WebP files in the **public** bucket `product-images` (override with `SUPABASE_PUBLIC_BUCKET`):

| File | Size | Use |
|---|---|---|
| `products/<hash>/card.webp` | longest side 480 px | product grids |
| `products/<hash>/detail.webp` | longest side 1600 px, never rendered above 4x the PDF's size | product page |

- `product.cropped_path` holds `products/<hash>`. The storefront builds the URL as `<SUPABASE_URL>/storage/v1/object/public/product-images/<cropped_path>/card.webp`. Files are served with a one-year immutable cache header, which is safe because `<hash>` comes from the PDF, page and crop, so a name never changes meaning.
- A product reviewed as "no image" (`crop = {"none": true}`) gets no files and keeps `cropped_path` null; the storefront shows a placeholder.
- When a reviewer changes a crop, Finish clears `cropped_path`, and the next run cuts the new image. Old files stay in the bucket.
- Safe to repeat and to interrupt: names are deterministic, uploads overwrite, and a product is marked published only after both files are stored and only if its crop did not change meanwhile. A failed product stays pending and is retried by the next run; the command exits `1` if any failed, and `5` if storage is unreachable.
- Crops are fractions of the page as displayed, so rotated pages work (tested at 90, 180 and 270 degrees).

## Configuration

| Setting | Where | Default |
|---|---|---|
| Catalogue folder | `PIM_CATALOG_DIR`, or `catalog_dir` in `config/settings.yaml` | `extraction/data/catalogs` |
| Local storage folder | `PIM_OUTPUT_DIR`, or `output_dir` in `config/settings.yaml` | `extraction/data/runs`; used only when Supabase Storage is not configured |
| Storage bucket (PDFs, private) | `SUPABASE_STORAGE_BUCKET` | `catalogs` |
| Storage bucket (product images, public) | `SUPABASE_PUBLIC_BUCKET` | `product-images` |
| Render resolution | `render_dpi` in `config/settings.yaml` | 150 |
| Batch size limits | `batch` in `config/settings.yaml` | 10,000 requests, 100 MB (a 162 MB upload dropped its connection) |
| Model and prices | `config/models.yaml` | `claude-sonnet-5`, batch discount 0.5 |

Each catalogue is a PDF named after its brand: `Jack_Jones.pdf` is catalogue `Jack_Jones` and brand "Jack Jones". The catalogue and brand are registered automatically at the first `batch submit`. A wrong brand name can be fixed at review. A re-uploaded PDF is recognised by its SHA-256, whatever it is called.

## Commands

Run from `extraction/`. Replace `Turtle` with a catalogue key. Batch is the only extraction mode; the old synchronous `run` command was removed.

| Command | What it does |
|---|---|
| `python -m pipeline list` | Lists PDFs in the catalogue folder with page counts. |
| `python -m pipeline report [catalogues] [--detail]` | Pages, cost and tokens per catalogue, plus a batch table. Reads the database; no API calls. |
| `python -m pipeline benchmark [catalogues] [options]` | Cost, latency, reliability and accuracy numbers. See Benchmarks. |
| `python -m pipeline batch submit [catalogues] [--pages 1,3-5]` | Sends pending pages as batches at half price. |
| `python -m pipeline batch status` | Reconciles unresolved submissions, then shows batch progress. |
| `python -m pipeline batch collect` | Writes results for ended batches. |
| `python -m pipeline batch sync` | Reconcile, status and collect in one run. Suitable for a schedule. |
| `python -m pipeline batch resolve ID --adopt BATCH` | Records a batch for a submission that needs review, if its results match. |
| `python -m pipeline batch resolve ID --release --confirm-no-batch` | Frees a submission's pages after confirming no batch exists. |
| `python -m pipeline storage check` | Shows which storage backend is used, creates the private and public buckets if needed, and tests both. |
| `python -m pipeline publish` | Cuts product images from the PDFs of fully reviewed catalogues and publishes them (see Publishing images). |
| `python -m pipeline db check` / `db migrate` | See Setup. |

Batch commands accept `--model`. Exit codes for `batch` commands: `0` success, `1` failed pages or invalid resolve arguments, `2` invalid API key, `3` unresolved or unknown submission, `4` another batch command holds the lock, `5` storage failed.

## How a batch flows

1. **Submit.** A catalogue's PDF is uploaded to storage when it is first registered. Each pending page is rendered and, in one transaction, reserved together with its submission row. The create call follows. Once Anthropic accepts the batch, its id, the `submitted` state and the pages' status are recorded in one transaction.
2. **Collect.** For each ended batch, every page is written in one statement: status, token and cost attempt row, raw model output, and product rows. Collect writes database rows only. It needs no PDF and no storage, so it can run on any machine that can reach the database and the Anthropic API. No page images are made at this stage.
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
| Catalogue PDFs | storage `pdfs/<sha256>.pdf`, key in `catalog.storage_path` |

Weights such as "247 gms" are parsed into a number and unit only when clearly valid; otherwise they are left empty and remain in the raw output. A product's subcategory is set only when the page prints both a category and a subcategory.

## Benchmarks

`python -m pipeline benchmark` prints every number in one report, grouped by how far it can be trusted. It makes no API calls.

| Group | Numbers | Needs |
|---|---|---|
| Measured | cost per page, per product, wasted spend on failed tries, batch turnaround p50/p95 and min/max, pages per hour, page success, retry and unresolved-submission rates, price-not-found and low-confidence shares | the database only |
| Reviewed | field and product correction rate, deleted and added rows (product table compared with the raw model output) | catalogues with level-1 review done; not available until the review UI exists |
| Audited | name precision, recall and F1, missing-product rate, price accuracy, price fabrication rate, correction rate, error rate by confidence | a filled-in audit sheet |
| Business | hours saved | `--manual-min-per-product` and `--assisted-min-per-product`, timed by a person |

Every rate shows its sample size and a 95% confidence interval, so a small sample cannot look more certain than it is. Batch turnaround percentiles need many batches; with fewer than 20 the report says to read min and max.

Audit workflow:

```powershell
python -m pipeline benchmark --export-audit audit.csv --audit-size 100
# open audit.csv: for each product set name_correct (y/n) and price_check
# (correct, wrong, fabricated, missing, none); on the first row of each page set
# missed_on_page (0 if no product was missed)
python -m pipeline benchmark --audit audit.csv --project-pages 5000
```

`--project-pages N` scales the measured cost per page to N pages; use the size of the catalogue set you want to price. The audit sample is random and spread across catalogues; the same `--seed` reproduces it. Non-product pages are included so missed products can be found. The export refuses to overwrite an existing sheet. Add `--json out.json` to keep the numbers.

## Tests

From the repository root:

```powershell
python -m pytest extraction/tests -q
```

GitHub Actions runs the same suite on every push, against a throwaway Postgres 17 container (`.github/workflows/tests.yml`), so it needs no Supabase project and no secrets. On a plain Postgres, `extraction/tests/supabase_shim.sql` supplies the roles and `auth` objects that Supabase provides; it is skipped on a real Supabase project. Tests that need the database are skipped when `DATABASE_URL` is unset. They run in a throwaway schema that is dropped afterwards, so real data is never touched. The suite uses a fake Batch API and a local storage folder, so it costs no API money and does not touch your bucket, but each database statement is a network round trip, so a full run takes several minutes against a remote project.

Two real-API scripts are run by hand, and each creates a real batch that writes to the database named in `.env`:

- `python tests/real_crash_child.py Turtle --pages 1-3`: exits after a batch is created, to exercise recovery.
- `python tests/real_release_child.py Turtle --pages 1-3`: exits before the create call, to exercise release.

## Known limits

- Tested on Windows only. The GitHub Actions job (Linux, plain Postgres) is written but has not run yet, so the Supabase shim is unverified until the first push.
- A live 429 response and a batch's 24-hour expiry are covered by unit tests only.
- The Supabase Storage backend is tested against a mock server only. Run `python -m pipeline storage check` against the real project to verify the keys and the bucket.
- Supabase's free plan allows 50 MB per uploaded file by default; a larger PDF needs a higher limit or resumable uploads.
- Page type (product or not) is decided by the model. Summary and introduction pages can be misclassified; reviewers check every page.
