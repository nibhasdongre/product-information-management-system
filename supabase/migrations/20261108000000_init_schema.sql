-- Core schema for the catalogue pipeline and batch processing.
-- Users and reviewer foreign keys, row-level security, and embeddings come in later migrations.
-- Brands. name_key makes "Borosil" and "borosil " the same brand.
create table brand (
  brand_id    bigint generated always as identity primary key,
  brand_name  text not null,
  name_key    text generated always as (lower(regexp_replace(btrim(brand_name), '\s+', ' ', 'g'))) stored,
  constraint brand_name_key_unique unique (name_key)
);

create table category (
  category_id   bigint generated always as identity primary key,
  category_name text not null unique
);

create table subcategory (
  sub_id       bigint generated always as identity primary key,
  subcat_name  text not null,
  category_id  bigint not null references category (category_id),
  constraint subcategory_name_unique unique (category_id, subcat_name)
);

-- One row per uploaded PDF. file_sha256 detects re-uploads.
create table catalog (
  catalog_id    bigint generated always as identity primary key,
  catalog_name  text not null,
  brand_id      bigint not null references brand (brand_id),
  file_sha256   char(64) not null unique,
  storage_path  text not null,
  uploaded_at   timestamptz not null default now()
);
create index catalog_brand_idx on catalog (brand_id);

create table batch_submissions (
  submission_id       uuid primary key default gen_random_uuid(),
  state               text not null check (state in
                        ('prepared', 'submitting', 'unknown', 'needs_review',
                         'submitted', 'completed', 'released')),
  anthropic_batch_id  text unique,
  candidate_batch_id  text,
  batch_created_at    timestamptz,
  batch_ended_at      timestamptz,
  next_check_at       timestamptz,
  check_attempts      int not null default 0,
  review_reason       text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  check (state not in ('submitted', 'completed') or anthropic_batch_id is not null)
);

create index batch_submissions_open_idx on batch_submissions (next_check_at)
  where state not in ('completed', 'released');


create table page_extractions (
  extraction_id  bigint generated always as identity primary key,
  catalog_id     bigint not null references catalog (catalog_id),
  page           int not null check (page >= 1),
  model          text not null,
  status         text not null check (status in
                   ('reserved', 'submitted', 'ok', 'error', 'released')),
  page_type      text check (page_type in ('product', 'non_product')),
  dpi            int not null,
  model_dpi      int not null,
  submission_id  uuid references batch_submissions (submission_id),
  updated_at     timestamptz not null default now(),
  constraint page_extractions_page_unique unique (catalog_id, page, model)
);
create index page_extractions_submission_idx on page_extractions (submission_id);
create index page_extractions_status_idx on page_extractions (status);


-- Raw model output for each page. Append-only: a correction means a new extraction.
create table extraction_data (
  extraction_id  bigint primary key references page_extractions (extraction_id),
  raw_output     jsonb not null,
  image_path     text
);

create function forbid_extraction_data_update() returns trigger
language plpgsql as $$
begin
  raise exception 'extraction_data is append-only';
end;
$$;

create trigger extraction_data_no_update
  before update on extraction_data
  for each row execute function forbid_extraction_data_update();

create table extraction_attempts (
  extraction_id  bigint not null references page_extractions (extraction_id),
  attempt_no     int not null check (attempt_no >= 1),
  submission_id  uuid references batch_submissions (submission_id),
  input_tokens   int not null default 0,
  output_tokens  int not null default 0,
  cost_usd       numeric(12, 6) not null default 0,
  render_s       numeric(10, 3),
  api_s          numeric(10, 3),
  error          text,
  created_at     timestamptz not null default now(),
  primary key (extraction_id, attempt_no)
);
create index extraction_attempts_submission_idx on extraction_attempts (submission_id)
  where submission_id is not null;


-- Working copy of each product. Reviewers edit this table.
create table product (
  product_id          bigint generated always as identity primary key,
  extraction_id       bigint not null references page_extractions (extraction_id),
  position            int not null check (position >= 1),
  product_name        text not null,
  product_description text,
  variant             text,
  price_amount        numeric(12, 2),
  price_currency      text not null default 'INR',
  tax_note            text,
  uom                 text,
  weight              numeric(12, 3),
  weight_unit         text,
  sub_id              bigint references subcategory (sub_id),
  confidence          numeric(4, 3) check (confidence between 0 and 1),
  extraction_flags    text[] not null default '{}',
  crop                jsonb,
  cropped_path        text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint product_position_unique unique (extraction_id, position)
);
create index product_sub_idx on product (sub_id);
create index product_price_idx on product (price_amount);
create index product_search_idx on product using gin (
  to_tsvector('english',
    coalesce(product_name, '') || ' ' || coalesce(product_description, ''))
);
-- Tags are stored lower-case. Normalise in the application before insert.
create table tag (
  tag_id    bigint generated always as identity primary key,
  tag_name  text not null unique check (tag_name = lower(btrim(tag_name)))
);

create table product_tag (
  product_id  bigint not null references product (product_id) on delete cascade,
  tag_id      bigint not null references tag (tag_id),
  primary key (product_id, tag_id)
);
create index product_tag_tag_idx on product_tag (tag_id);

-- One row per catalogue. Reviewers work through the whole catalogue, then mark the level done.
create table review (
  catalog_id     bigint primary key references catalog (catalog_id),
  reviewer_1_id  uuid,   -- references users once the users table exists
  level_1_at     timestamptz,
  reviewer_2_id  uuid,   -- references users once the users table exists
  level_2_at     timestamptz,
  check (level_1_at is null or reviewer_1_id is not null),
  check (level_2_at is null or (reviewer_2_id is not null and level_1_at is not null))
);

-- Row-level security. Supabase exposes every table in the public schema through its Data API,
-- and the anon key is shipped to browsers. With RLS on and no policies, anon and authenticated
-- requests see nothing. The worker connects as the postgres role, which bypasses RLS.
-- Reviewer and web-app policies arrive with the users migration.
alter table brand              enable row level security;
alter table category           enable row level security;
alter table subcategory        enable row level security;
alter table catalog            enable row level security;
alter table batch_submissions  enable row level security;
alter table page_extractions   enable row level security;
alter table extraction_data    enable row level security;
alter table extraction_attempts enable row level security;
alter table product            enable row level security;
alter table tag                enable row level security;
alter table product_tag        enable row level security;
alter table review             enable row level security;
