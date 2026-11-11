-- Review stage: users and roles, server-side drafts, audit trail, and the functions the review screen calls.
--
-- The review screen talks to the database only through the rv_* and admin_* functions below. They run as
-- the table owner (security definer) and check who is calling and what that person may do, so the rules
-- hold whatever the screen does. The tables stay locked (RLS on, no policies): a browser cannot read or
-- write them directly.
--
-- Review model, per catalogue:
--   * Ready when every page 1..page_count has an extraction (ok, or error = treated as a non-product page).
--   * Level 1, then Level 2, each a full pass by a different person. A level is claimed, worked, and finished.
--   * Edits are drafts (review_edit) until Finish, which checks that every page was viewed and every product
--     is valid, then applies all edits in one transaction and writes the audit trail (review_log).
--   * Crop is stored in product.crop as fractions of the page: null = whole page, {"none": true} = no image,
--     {"x","y","w","h"} = a rectangle. A changed crop clears product.cropped_path so the publisher redoes it.

alter table catalog add column page_count int check (page_count >= 1);
update catalog c set page_count = m.last_page
  from (select catalog_id, max(page) as last_page from page_extractions group by catalog_id) m
  where m.catalog_id = c.catalog_id;

create table app_user (
  user_id     uuid primary key references auth.users (id) on delete cascade,
  email       text not null,
  role        text not null check (role in ('reviewer', 'admin')),
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

alter table review add constraint review_reviewer_1_fk foreign key (reviewer_1_id) references app_user (user_id);
alter table review add constraint review_reviewer_2_fk foreign key (reviewer_2_id) references app_user (user_id);
alter table review add constraint review_two_people check (reviewer_2_id is null or reviewer_2_id <> reviewer_1_id);

-- Draft edits for one catalogue and level. update/remove point at an existing product; add is a new
-- product on a page; brand renames the catalogue's brand.
create table review_edit (
  edit_id     bigint generated always as identity primary key,
  catalog_id  bigint not null references catalog (catalog_id),
  level       int not null check (level in (1, 2)),
  kind        text not null check (kind in ('update', 'add', 'remove', 'brand')),
  product_id  bigint references product (product_id) on delete cascade,
  page        int check (page >= 1),
  payload     jsonb not null default '{}',
  user_id     uuid not null references app_user (user_id),
  updated_at  timestamptz not null default now(),
  check ((kind in ('update', 'remove')) = (product_id is not null)),
  check ((kind = 'add') = (page is not null))
);
create unique index review_edit_product_uq on review_edit (catalog_id, level, product_id) where product_id is not null;
create unique index review_edit_brand_uq on review_edit (catalog_id, level) where kind = 'brand';
create index review_edit_catalog_idx on review_edit (catalog_id, level);

create table review_page_seen (
  catalog_id  bigint not null references catalog (catalog_id),
  level       int not null check (level in (1, 2)),
  page        int not null check (page >= 1),
  seen_at     timestamptz not null default now(),
  primary key (catalog_id, level, page)
);

-- Who changed what. Append-only. No foreign keys, so a row outlives anything it mentions.
create table review_log (
  log_id      bigint generated always as identity primary key,
  at          timestamptz not null default now(),
  user_id     uuid not null,
  catalog_id  bigint,
  level       int,
  action      text not null,
  product_id  bigint,
  page        int,
  detail      jsonb
);
create index review_log_catalog_idx on review_log (catalog_id, at);

create function forbid_review_log_change() returns trigger
language plpgsql as $$
begin
  raise exception 'review_log is append-only';
end;
$$;
create trigger review_log_append_only before update or delete on review_log
  for each row execute function forbid_review_log_change();

alter table app_user         enable row level security;
alter table review_edit      enable row level security;
alter table review_page_seen enable row level security;
alter table review_log       enable row level security;

-- ---------------------------------------------------------------------------------------------
-- Internal helpers (rvx_*). Not callable from a browser.

create function rvx_actor() returns app_user
language plpgsql stable security definer set search_path from current as $$
declare u app_user;
begin
  select * into u from app_user where user_id = auth.uid() and active;
  if not found then
    raise exception 'not an active user' using errcode = '42501';
  end if;
  return u;
end;
$$;

create function rvx_admin() returns app_user
language plpgsql stable security definer set search_path from current as $$
declare u app_user;
begin
  u := rvx_actor();
  if u.role <> 'admin' then
    raise exception 'admin only' using errcode = '42501';
  end if;
  return u;
end;
$$;

create function rvx_log(cid bigint, lvl int, act text, pid bigint, pg int, det jsonb) returns void
language sql security definer set search_path from current as $$
  insert into review_log (user_id, catalog_id, level, action, product_id, page, detail)
  values (auth.uid(), cid, lvl, act, pid, pg, det);
$$;

-- The caller must be the person who claimed this level, and the level must not be finished.
create function rvx_hold(cid bigint, lvl int) returns app_user
language plpgsql stable security definer set search_path from current as $$
declare u app_user; r review; holder uuid; done timestamptz;
begin
  u := rvx_actor();
  if lvl is null or lvl not in (1, 2) then
    raise exception 'level must be 1 or 2';
  end if;
  select * into r from review where catalog_id = cid;
  holder := case lvl when 1 then r.reviewer_1_id else r.reviewer_2_id end;
  done := case lvl when 1 then r.level_1_at else r.level_2_at end;
  if not found or holder is distinct from u.user_id then
    raise exception 'you have not opened level % of this catalogue', lvl using errcode = '42501';
  end if;
  if done is not null then
    raise exception 'level % of this catalogue is already finished', lvl;
  end if;
  return u;
end;
$$;

create function rvx_ready(cid bigint) returns boolean
language sql stable security definer set search_path from current as $$
  select coalesce((
    select c.page_count is not null and not exists (
      select 1 from generate_series(1, c.page_count) g(p)
      where not exists (select 1 from page_extractions e
                        where e.catalog_id = c.catalog_id and e.page = g.p and e.status in ('ok', 'error')))
    from catalog c where c.catalog_id = cid), false)
$$;

-- The extraction the reviewer sees for a page: an ok one if there is one, else a failed one.
create function rvx_page_extraction(cid bigint, pg int) returns bigint
language sql stable security definer set search_path from current as $$
  select extraction_id from page_extractions
  where catalog_id = cid and page = pg and status in ('ok', 'error')
  order by (status = 'ok') desc, extraction_id limit 1
$$;

-- What a product looks like to the reviewer: the stored row with the draft payload laid over it.
create function rvx_product_json(p product, pg int, payload jsonb) returns jsonb
language sql stable security definer set search_path from current as $$
  select jsonb_build_object(
    'product_id', p.product_id, 'edit_id', null::bigint, 'page', pg, 'position', p.position,
    'product_name', p.product_name, 'product_description', p.product_description, 'variant', p.variant,
    'price_amount', p.price_amount, 'price_currency', p.price_currency, 'tax_note', p.tax_note,
    'uom', p.uom, 'weight', p.weight, 'weight_unit', p.weight_unit, 'sub_id', p.sub_id,
    'confidence', p.confidence, 'extraction_flags', to_jsonb(p.extraction_flags), 'crop', p.crop,
    'tags', coalesce((select jsonb_agg(t.tag_name order by t.tag_name)
                      from product_tag pt join tag t using (tag_id) where pt.product_id = p.product_id), '[]'::jsonb)
  ) || coalesce(payload, '{}'::jsonb)
$$;

create function rvx_add_json(e review_edit) returns jsonb
language sql stable security definer set search_path from current as $$
  select jsonb_build_object(
    'product_id', null::bigint, 'edit_id', e.edit_id, 'page', e.page, 'position', null::int,
    'product_name', null::text, 'product_description', null::text, 'variant', null::text,
    'price_amount', null::numeric, 'price_currency', 'INR', 'tax_note', null::text, 'uom', null::text,
    'weight', null::numeric, 'weight_unit', null::text, 'sub_id', null::bigint, 'confidence', null::numeric,
    'extraction_flags', '[]'::jsonb, 'crop', null::jsonb, 'tags', '[]'::jsonb
  ) || e.payload
$$;

-- Every product the catalogue would have if the draft were applied now (removed ones left out).
create function rvx_effective(cid bigint, lvl int)
returns table (page int, pos int, product_id bigint, edit_id bigint, doc jsonb)
language sql stable security definer set search_path from current as $$
  select x.page, p.position, p.product_id, null::bigint, rvx_product_json(p, x.page, u.payload)
  from product p
  join page_extractions x on x.extraction_id = p.extraction_id
  left join review_edit u on u.catalog_id = cid and u.level = lvl and u.kind = 'update' and u.product_id = p.product_id
  where x.catalog_id = cid
    and x.extraction_id = rvx_page_extraction(cid, x.page)
    and not exists (select 1 from review_edit r
                    where r.catalog_id = cid and r.level = lvl and r.kind = 'remove' and r.product_id = p.product_id)
  union all
  select a.page, 1000000 + a.edit_id::int, null::bigint, a.edit_id, rvx_add_json(a)
  from review_edit a where a.catalog_id = cid and a.level = lvl and a.kind = 'add'
$$;

-- Validate and normalise product fields sent by the screen. Unknown keys are refused.
create function rvx_clean(f jsonb) returns jsonb
language plpgsql stable security definer set search_path from current as $$
declare
  k text; v jsonb; res jsonb := '{}'::jsonb; s text; n numeric; tags text[];
  cx numeric; cy numeric; cw numeric; ch numeric;
begin
  if f is null or jsonb_typeof(f) <> 'object' then
    raise exception 'fields must be a JSON object';
  end if;
  for k, v in select key, value from jsonb_each(f) loop
    if k = 'product_name' then
      if jsonb_typeof(v) <> 'string' or btrim(v #>> '{}') = '' then
        raise exception 'product_name must be non-empty text';
      end if;
      s := btrim(v #>> '{}');
      if length(s) > 300 then raise exception 'product_name is too long (300 characters at most)'; end if;
      res := res || jsonb_build_object(k, s);
    elsif k = 'price_currency' then
      if jsonb_typeof(v) <> 'string' or btrim(v #>> '{}') = '' then
        raise exception 'price_currency must be non-empty text';
      end if;
      s := upper(btrim(v #>> '{}'));
      if length(s) > 8 then raise exception 'price_currency is too long'; end if;
      res := res || jsonb_build_object(k, s);
    elsif k in ('product_description', 'variant', 'tax_note', 'uom', 'weight_unit') then
      if jsonb_typeof(v) = 'null' then
        res := res || jsonb_build_object(k, null::text);
      elsif jsonb_typeof(v) <> 'string' then
        raise exception '% must be text or null', k;
      else
        s := btrim(v #>> '{}');
        if length(s) > 4000 then raise exception '% is too long (4000 characters at most)', k; end if;
        res := res || jsonb_build_object(k, nullif(s, ''));
      end if;
    elsif k in ('price_amount', 'weight') then
      if jsonb_typeof(v) = 'null' then
        res := res || jsonb_build_object(k, null::numeric);
      elsif jsonb_typeof(v) <> 'number' then
        raise exception '% must be a number or null', k;
      else
        n := (v #>> '{}')::numeric;
        if n < 0 or n >= 1000000000 then raise exception '% is out of range', k; end if;
        res := res || jsonb_build_object(k, round(n, case when k = 'price_amount' then 2 else 3 end));
      end if;
    elsif k = 'sub_id' then
      if jsonb_typeof(v) = 'null' then
        res := res || jsonb_build_object(k, null::bigint);
      elsif jsonb_typeof(v) <> 'number' or (v #>> '{}') !~ '^[0-9]+$'
            or not exists (select 1 from subcategory where sub_id = (v #>> '{}')::bigint) then
        raise exception 'sub_id is not an existing subcategory';
      else
        res := res || jsonb_build_object(k, (v #>> '{}')::bigint);
      end if;
    elsif k = 'tags' then
      if jsonb_typeof(v) <> 'array'
         or exists (select 1 from jsonb_array_elements(v) e where jsonb_typeof(e) <> 'string') then
        raise exception 'tags must be a list of text';
      end if;
      select coalesce(array_agg(distinct q.t order by q.t), '{}') into tags
        from (select lower(btrim(e #>> '{}')) as t from jsonb_array_elements(v) e) q where q.t <> '';
      if cardinality(tags) > 30 then raise exception 'too many tags (30 at most)'; end if;
      if exists (select 1 from unnest(tags) z where length(z) > 50) then
        raise exception 'a tag is too long (50 characters at most)';
      end if;
      res := res || jsonb_build_object(k, to_jsonb(tags));
    elsif k = 'crop' then
      if jsonb_typeof(v) = 'null' then
        res := res || jsonb_build_object(k, null::jsonb);
      elsif v = '{"none": true}'::jsonb then
        res := res || jsonb_build_object(k, v);
      elsif jsonb_typeof(v) = 'object' and (select count(*) from jsonb_object_keys(v)) = 4
            and v ?& array['x', 'y', 'w', 'h']
            and jsonb_typeof(v -> 'x') = 'number' and jsonb_typeof(v -> 'y') = 'number'
            and jsonb_typeof(v -> 'w') = 'number' and jsonb_typeof(v -> 'h') = 'number' then
        cx := (v ->> 'x')::numeric; cy := (v ->> 'y')::numeric;
        cw := (v ->> 'w')::numeric; ch := (v ->> 'h')::numeric;
        if cx < 0 or cy < 0 or cw < 0.01 or ch < 0.01 or cx + cw > 1.0001 or cy + ch > 1.0001 then
          raise exception 'crop must lie inside the page (x, y, w, h as fractions of the page, at least 1%% wide and high)';
        end if;
        res := res || jsonb_build_object(k, jsonb_build_object(
          'x', round(cx, 5), 'y', round(cy, 5), 'w', round(cw, 5), 'h', round(ch, 5)));
      else
        raise exception 'crop must be null, {"none": true}, or {x, y, w, h} as fractions of the page';
      end if;
    else
      raise exception 'unknown field %', k;
    end if;
  end loop;
  return res;
end;
$$;

create function rvx_set_tags(pid bigint, tags jsonb) returns void
language sql security definer set search_path from current as $$
  delete from product_tag where product_id = pid;
  insert into tag (tag_name) select jsonb_array_elements_text(tags) on conflict do nothing;
  insert into product_tag (product_id, tag_id)
    select pid, t.tag_id from tag t where t.tag_name in (select jsonb_array_elements_text(tags));
$$;

-- What the caller may do next on a catalogue. r is its review row (all null if there is none yet).
create function rvx_next(r review, uid uuid) returns jsonb
language plpgsql stable as $$
begin
  if r.level_1_at is null then
    if r.reviewer_1_id is null then
      return jsonb_build_object('level', 1, 'action', 'claim');
    elsif r.reviewer_1_id = uid then
      return jsonb_build_object('level', 1, 'action', 'resume');
    end if;
    return jsonb_build_object('level', 1, 'action', 'blocked', 'reason', 'Level 1 is being done by another reviewer');
  elsif r.level_2_at is null then
    if r.reviewer_2_id is null then
      if r.reviewer_1_id = uid then
        return jsonb_build_object('level', 2, 'action', 'blocked',
                                  'reason', 'Level 2 must be done by a different person than Level 1');
      end if;
      return jsonb_build_object('level', 2, 'action', 'claim');
    elsif r.reviewer_2_id = uid then
      return jsonb_build_object('level', 2, 'action', 'resume');
    end if;
    return jsonb_build_object('level', 2, 'action', 'blocked', 'reason', 'Level 2 is being done by another reviewer');
  end if;
  return jsonb_build_object('action', 'blocked', 'reason', 'This catalogue has finished both review levels');
end;
$$;

-- ---------------------------------------------------------------------------------------------
-- Review functions the screen calls.

create function rv_me() returns jsonb
language plpgsql stable security definer set search_path from current as $$
declare u app_user;
begin
  u := rvx_actor();
  return jsonb_build_object('user_id', u.user_id, 'email', u.email, 'role', u.role);
end;
$$;

create function rv_lists() returns jsonb
language plpgsql stable security definer set search_path from current as $$
begin
  perform rvx_actor();
  return jsonb_build_object(
    'brands', coalesce((select jsonb_agg(jsonb_build_object('id', brand_id, 'name', brand_name) order by name_key)
                        from brand), '[]'::jsonb),
    'categories', coalesce((select jsonb_agg(jsonb_build_object('id', category_id, 'name', category_name)
                                             order by category_name) from category), '[]'::jsonb),
    'subcategories', coalesce((select jsonb_agg(jsonb_build_object('id', sub_id, 'name', subcat_name,
                                                                   'category_id', category_id)
                                                order by subcat_name) from subcategory), '[]'::jsonb),
    'tags', coalesce((select jsonb_agg(tag_name order by tag_name) from tag), '[]'::jsonb));
end;
$$;

create function rv_add_category(cat_name text) returns jsonb
language plpgsql security definer set search_path from current as $$
declare s text := btrim(coalesce(cat_name, '')); cid bigint;
begin
  perform rvx_actor();
  if s = '' or length(s) > 100 then raise exception 'category name must be 1 to 100 characters'; end if;
  select category_id into cid from category where lower(category_name) = lower(s);
  if cid is null then
    insert into category (category_name) values (s) returning category_id into cid;
    perform rvx_log(null, null, 'add_category', null, null, jsonb_build_object('category_id', cid, 'name', s));
  end if;
  return jsonb_build_object('id', cid, 'name', (select category_name from category where category_id = cid));
end;
$$;

create function rv_add_subcategory(cat_id bigint, sub_name text) returns jsonb
language plpgsql security definer set search_path from current as $$
declare s text := btrim(coalesce(sub_name, '')); sid bigint;
begin
  perform rvx_actor();
  if s = '' or length(s) > 100 then raise exception 'subcategory name must be 1 to 100 characters'; end if;
  if not exists (select 1 from category where category_id = cat_id) then
    raise exception 'unknown category';
  end if;
  select sub_id into sid from subcategory where category_id = cat_id and lower(subcat_name) = lower(s);
  if sid is null then
    insert into subcategory (subcat_name, category_id) values (s, cat_id) returning sub_id into sid;
    perform rvx_log(null, null, 'add_subcategory', null, null,
                    jsonb_build_object('sub_id', sid, 'category_id', cat_id, 'name', s));
  end if;
  return jsonb_build_object('id', sid, 'name', (select subcat_name from subcategory where sub_id = sid),
                            'category_id', cat_id);
end;
$$;

create function rv_list_catalogs() returns jsonb
language plpgsql stable security definer set search_path from current as $$
declare u app_user; out jsonb := '[]'::jsonb; c record; r review; nx jsonb; ready boolean; st text;
begin
  u := rvx_actor();
  for c in select ca.catalog_id, ca.catalog_name, ca.page_count, b.brand_name
           from catalog ca join brand b using (brand_id) order by ca.catalog_name, ca.catalog_id loop
    select * into r from review where catalog_id = c.catalog_id;
    ready := rvx_ready(c.catalog_id);
    nx := rvx_next(r, u.user_id);
    st := case
      when not ready then 'not_ready'
      when r.level_2_at is not null then 'done'
      when r.level_1_at is null and r.reviewer_1_id is null then 'level_1_open'
      when r.level_1_at is null then 'level_1_in_progress'
      when r.reviewer_2_id is null then 'level_2_open'
      else 'level_2_in_progress' end;
    out := out || jsonb_build_array(jsonb_build_object(
      'catalog_id', c.catalog_id, 'catalog_name', c.catalog_name, 'brand_name', c.brand_name,
      'page_count', c.page_count, 'ready', ready, 'state', st,
      'level_1_by', (select email from app_user where user_id = r.reviewer_1_id), 'level_1_at', r.level_1_at,
      'level_2_by', (select email from app_user where user_id = r.reviewer_2_id), 'level_2_at', r.level_2_at,
      'can_open', ready and nx ->> 'action' in ('claim', 'resume'),
      'mine', ready and nx ->> 'action' = 'resume',
      'level', case when ready then nx -> 'level' end,
      'blocked_reason', case when ready and nx ->> 'action' = 'blocked' then nx ->> 'reason' end));
  end loop;
  return out;
end;
$$;

create function rv_progress(cid bigint, lvl int) returns jsonb
language plpgsql stable security definer set search_path from current as $$
declare
  u app_user; c record; seen int[]; per_page jsonb; draft_brand text; pc int; edits int;
begin
  u := rvx_hold(cid, lvl);
  select ca.catalog_name, ca.storage_path, ca.page_count, b.brand_name into c
    from catalog ca join brand b using (brand_id) where ca.catalog_id = cid;
  pc := c.page_count;
  seen := array(select page from review_page_seen where catalog_id = cid and level = lvl order by page);
  select coalesce(jsonb_agg(coalesce(q.n, 0) order by g.p), '[]'::jsonb) into per_page
    from generate_series(1, pc) g(p)
    left join (select page, count(*) as n from rvx_effective(cid, lvl) group by page) q on q.page = g.p;
  select payload ->> 'brand_name' into draft_brand
    from review_edit where catalog_id = cid and level = lvl and kind = 'brand';
  select count(*) into edits from review_edit where catalog_id = cid and level = lvl;
  return jsonb_build_object(
    'catalog_id', cid, 'catalog_name', c.catalog_name, 'storage_path', c.storage_path, 'level', lvl,
    'page_count', pc, 'brand_name', coalesce(draft_brand, c.brand_name),
    'seen_pages', to_jsonb(seen), 'pages_seen', cardinality(seen),
    'can_finish', cardinality(seen) = pc,
    'resume_page', coalesce((select min(g.p) from generate_series(1, pc) g(p) where g.p <> all (seen)), 1),
    'products_per_page', per_page, 'product_count', (select coalesce(sum(value::int), 0) from jsonb_array_elements_text(per_page) value),
    'edit_count', edits);
end;
$$;

-- Open the catalogue for the next level this person may do: claims it, or resumes their own work.
create function rv_open(cid bigint) returns jsonb
language plpgsql security definer set search_path from current as $$
declare u app_user; r review; nx jsonb;
begin
  u := rvx_actor();
  if not exists (select 1 from catalog where catalog_id = cid) then
    raise exception 'unknown catalogue';
  end if;
  if not rvx_ready(cid) then
    raise exception 'this catalogue is not ready for review: some pages have not been extracted yet';
  end if;
  insert into review (catalog_id) values (cid) on conflict do nothing;
  select * into r from review where catalog_id = cid for update;
  nx := rvx_next(r, u.user_id);
  if nx ->> 'action' = 'blocked' then
    raise exception '%', nx ->> 'reason';
  end if;
  if nx ->> 'action' = 'claim' then
    if (nx ->> 'level')::int = 1 then
      update review set reviewer_1_id = u.user_id where catalog_id = cid;
    else
      update review set reviewer_2_id = u.user_id where catalog_id = cid;
    end if;
    perform rvx_log(cid, (nx ->> 'level')::int, 'claim', null, null, null);
  end if;
  return rv_progress(cid, (nx ->> 'level')::int);
end;
$$;

-- Show one page and record that it was viewed. A page whose extraction failed is a non-product page.
create function rv_get_page(cid bigint, lvl int, pg int) returns jsonb
language plpgsql security definer set search_path from current as $$
declare u app_user; pc int; ex bigint; ex_status text; ptype text; prods jsonb;
begin
  u := rvx_hold(cid, lvl);
  select page_count into pc from catalog where catalog_id = cid;
  if pg is null or pg < 1 or pg > pc then
    raise exception 'page must be between 1 and %', pc;
  end if;
  insert into review_page_seen (catalog_id, level, page) values (cid, lvl, pg) on conflict do nothing;
  ex := rvx_page_extraction(cid, pg);
  select status, page_type into ex_status, ptype from page_extractions where extraction_id = ex;
  select coalesce(jsonb_agg(x.doc order by x.sort), '[]'::jsonb) into prods from (
    select p.position as sort,
           rvx_product_json(p, pg, up.payload)
             || jsonb_build_object('removed', rm.edit_id is not null, 'edited', up.edit_id is not null) as doc
    from product p
    left join review_edit up on up.catalog_id = cid and up.level = lvl and up.kind = 'update' and up.product_id = p.product_id
    left join review_edit rm on rm.catalog_id = cid and rm.level = lvl and rm.kind = 'remove' and rm.product_id = p.product_id
    where p.extraction_id = ex
    union all
    select 1000000 + a.edit_id, rvx_add_json(a) || jsonb_build_object('removed', false, 'edited', true)
    from review_edit a where a.catalog_id = cid and a.level = lvl and a.kind = 'add' and a.page = pg
  ) x;
  return jsonb_build_object('page', pg, 'page_count', pc, 'failed', ex_status = 'error',
                            'page_type', case when ex_status = 'ok' then coalesce(ptype, 'product') else 'non_product' end,
                            'products', prods);
end;
$$;

-- Save fields of an existing product (product_id) or of a product added in this draft (edit_id).
create function rv_save_product(cid bigint, lvl int, pid bigint, eid bigint, fields jsonb) returns jsonb
language plpgsql security definer set search_path from current as $$
declare u app_user; clean jsonb; p product; pg int; merged jsonb; a review_edit;
begin
  u := rvx_hold(cid, lvl);
  if (pid is null) = (eid is null) then
    raise exception 'give exactly one of product_id and edit_id';
  end if;
  clean := rvx_clean(fields);
  if pid is not null then
    select pr.* into p from product pr join page_extractions x using (extraction_id)
      where pr.product_id = pid and x.catalog_id = cid;
    if not found then raise exception 'that product is not in this catalogue'; end if;
    select page into pg from page_extractions where extraction_id = p.extraction_id;
    if exists (select 1 from review_edit where catalog_id = cid and level = lvl and kind = 'remove' and product_id = pid) then
      raise exception 'that product is marked removed; restore it first';
    end if;
    insert into review_edit (catalog_id, level, kind, product_id, payload, user_id)
      values (cid, lvl, 'update', pid, clean, u.user_id)
      on conflict (catalog_id, level, product_id) where product_id is not null
      do update set payload = review_edit.payload || excluded.payload, user_id = excluded.user_id, updated_at = now()
      returning payload into merged;
    return rvx_product_json(p, pg, merged) || jsonb_build_object('removed', false, 'edited', true);
  end if;
  update review_edit set payload = payload || clean, user_id = u.user_id, updated_at = now()
    where edit_id = eid and catalog_id = cid and level = lvl and kind = 'add' returning * into a;
  if not found then raise exception 'that added product does not exist in this draft'; end if;
  return rvx_add_json(a) || jsonb_build_object('removed', false, 'edited', true);
end;
$$;

create function rv_add_product(cid bigint, lvl int, pg int, fields jsonb default '{}') returns jsonb
language plpgsql security definer set search_path from current as $$
declare u app_user; pc int; a review_edit;
begin
  u := rvx_hold(cid, lvl);
  select page_count into pc from catalog where catalog_id = cid;
  if pg is null or pg < 1 or pg > pc then
    raise exception 'page must be between 1 and %', pc;
  end if;
  insert into review_edit (catalog_id, level, kind, page, payload, user_id)
    values (cid, lvl, 'add', pg, rvx_clean(fields), u.user_id) returning * into a;
  return rvx_add_json(a) || jsonb_build_object('removed', false, 'edited', true);
end;
$$;

-- Remove an existing product (it can be restored until Finish) or discard one added in this draft.
create function rv_remove_product(cid bigint, lvl int, pid bigint, eid bigint) returns void
language plpgsql security definer set search_path from current as $$
declare u app_user;
begin
  u := rvx_hold(cid, lvl);
  if (pid is null) = (eid is null) then
    raise exception 'give exactly one of product_id and edit_id';
  end if;
  if pid is not null then
    if not exists (select 1 from product pr join page_extractions x using (extraction_id)
                   where pr.product_id = pid and x.catalog_id = cid) then
      raise exception 'that product is not in this catalogue';
    end if;
    delete from review_edit where catalog_id = cid and level = lvl and kind = 'update' and product_id = pid;
    insert into review_edit (catalog_id, level, kind, product_id, user_id) values (cid, lvl, 'remove', pid, u.user_id)
      on conflict (catalog_id, level, product_id) where product_id is not null do nothing;
  else
    delete from review_edit where edit_id = eid and catalog_id = cid and level = lvl and kind = 'add';
    if not found then raise exception 'that added product does not exist in this draft'; end if;
  end if;
end;
$$;

create function rv_restore_product(cid bigint, lvl int, pid bigint) returns void
language plpgsql security definer set search_path from current as $$
begin
  perform rvx_hold(cid, lvl);
  delete from review_edit where catalog_id = cid and level = lvl and kind = 'remove' and product_id = pid;
  if not found then raise exception 'that product is not marked removed'; end if;
end;
$$;

-- Set the catalogue's brand (applied at Finish). Naming the current brand again clears the change.
create function rv_set_brand(cid bigint, lvl int, new_name text) returns void
language plpgsql security definer set search_path from current as $$
declare u app_user; s text := btrim(coalesce(new_name, ''));
begin
  u := rvx_hold(cid, lvl);
  if s = '' or length(s) > 200 then raise exception 'brand name must be 1 to 200 characters'; end if;
  delete from review_edit where catalog_id = cid and level = lvl and kind = 'brand';
  if not exists (select 1 from catalog c join brand b using (brand_id) where c.catalog_id = cid
                 and b.name_key = lower(regexp_replace(s, '\s+', ' ', 'g'))) then
    insert into review_edit (catalog_id, level, kind, payload, user_id)
      values (cid, lvl, 'brand', jsonb_build_object('brand_name', s), u.user_id);
  end if;
end;
$$;

-- Finish a level: every page viewed, every product valid, then apply all edits at once.
create function rv_finish(cid bigint, lvl int) returns jsonb
language plpgsql security definer set search_path from current as $$
declare
  u app_user; r review; pc int; missing int[]; problems text; e review_edit; p product; pg int;
  d record; v0 jsonb; v1 jsonb; before jsonb; after jsonb; bname text; new_brand bigint; old_brand bigint;
  n_upd int := 0; n_add int := 0; n_rem int := 0; new_pid bigint; ex bigint;
begin
  perform rvx_hold(cid, lvl);
  select * into r from review where catalog_id = cid for update;
  u := rvx_hold(cid, lvl);                                   -- again, now that the row is locked
  select page_count into pc from catalog where catalog_id = cid;

  select array_agg(g.p order by g.p) into missing from generate_series(1, pc) g(p)
    where not exists (select 1 from review_page_seen s where s.catalog_id = cid and s.level = lvl and s.page = g.p);
  if missing is not null then
    raise exception 'pages not viewed yet: %', array_to_string(missing[1:20], ', ') || case when cardinality(missing) > 20 then ', ...' else '' end;
  end if;

  select string_agg(msg, '; ' order by page, pos) into problems from (
    select f.page, f.pos,
           'page ' || f.page || ' ' || coalesce('"' || (f.doc ->> 'product_name') || '"', '(unnamed product)') || ': ' ||
           concat_ws(', ',
             case when btrim(coalesce(f.doc ->> 'product_name', '')) = '' then 'name is missing' end,
             case when jsonb_typeof(f.doc -> 'price_amount') is distinct from 'number' then 'price is missing' end,
             case when btrim(coalesce(f.doc ->> 'price_currency', '')) = '' then 'currency is missing' end) as msg
    from rvx_effective(cid, lvl) f
    where btrim(coalesce(f.doc ->> 'product_name', '')) = ''
       or jsonb_typeof(f.doc -> 'price_amount') is distinct from 'number'
       or btrim(coalesce(f.doc ->> 'price_currency', '')) = ''
    limit 10) q;
  if problems is not null then
    raise exception 'fix these products before finishing: %', problems;
  end if;

  select payload ->> 'brand_name' into bname from review_edit where catalog_id = cid and level = lvl and kind = 'brand';
  if bname is not null then
    insert into brand (brand_name) values (bname) on conflict on constraint brand_name_key_unique do nothing;
    select brand_id into new_brand from brand where name_key = lower(regexp_replace(btrim(bname), '\s+', ' ', 'g'));
    select brand_id into old_brand from catalog where catalog_id = cid;
    if new_brand is distinct from old_brand then
      update catalog set brand_id = new_brand where catalog_id = cid;
      perform rvx_log(cid, lvl, 'brand', null, null, jsonb_build_object(
        'before', (select brand_name from brand where brand_id = old_brand), 'after', bname));
    end if;
  end if;

  for e in select * from review_edit where catalog_id = cid and level = lvl and kind = 'remove' loop
    select * into p from product where product_id = e.product_id;
    if found then
      select page into pg from page_extractions where extraction_id = p.extraction_id;
      perform rvx_log(cid, lvl, 'remove', p.product_id, pg, jsonb_build_object('before', rvx_product_json(p, pg, null)));
      delete from product where product_id = p.product_id;
      n_rem := n_rem + 1;
    end if;
  end loop;

  for e in select * from review_edit where catalog_id = cid and level = lvl and kind = 'update' order by edit_id loop
    select * into p from product where product_id = e.product_id;
    select page into pg from page_extractions where extraction_id = p.extraction_id;
    v0 := rvx_product_json(p, pg, null);
    v1 := v0 || e.payload;
    select jsonb_object_agg(k, v0 -> k), jsonb_object_agg(k, v1 -> k) into before, after
      from jsonb_object_keys(e.payload) k where (v0 -> k) is distinct from (v1 -> k);
    if before is not null then
      update product set
        product_name        = case when e.payload ? 'product_name' then e.payload ->> 'product_name' else product_name end,
        product_description = case when e.payload ? 'product_description' then e.payload ->> 'product_description' else product_description end,
        variant             = case when e.payload ? 'variant' then e.payload ->> 'variant' else variant end,
        price_amount        = case when e.payload ? 'price_amount' then (e.payload ->> 'price_amount')::numeric else price_amount end,
        price_currency      = case when e.payload ? 'price_currency' then e.payload ->> 'price_currency' else price_currency end,
        tax_note            = case when e.payload ? 'tax_note' then e.payload ->> 'tax_note' else tax_note end,
        uom                 = case when e.payload ? 'uom' then e.payload ->> 'uom' else uom end,
        weight              = case when e.payload ? 'weight' then (e.payload ->> 'weight')::numeric else weight end,
        weight_unit         = case when e.payload ? 'weight_unit' then e.payload ->> 'weight_unit' else weight_unit end,
        sub_id              = case when e.payload ? 'sub_id' then (e.payload ->> 'sub_id')::bigint else sub_id end,
        crop                = case when e.payload ? 'crop' then nullif(e.payload -> 'crop', 'null'::jsonb) else crop end,
        cropped_path        = case when before ? 'crop' then null else cropped_path end,
        updated_at          = now()
      where product_id = p.product_id;
      if before ? 'tags' then
        perform rvx_set_tags(p.product_id, e.payload -> 'tags');
      end if;
      perform rvx_log(cid, lvl, 'update', p.product_id, pg, jsonb_build_object('before', before, 'after', after));
      n_upd := n_upd + 1;
    end if;
  end loop;

  for e in select * from review_edit where catalog_id = cid and level = lvl and kind = 'add' order by edit_id loop
    ex := rvx_page_extraction(cid, e.page);
    insert into product (extraction_id, position, product_name, product_description, variant, price_amount,
                         price_currency, tax_note, uom, weight, weight_unit, sub_id, crop)
    select ex, coalesce((select max(position) from product where extraction_id = ex), 0) + 1,
           e.payload ->> 'product_name', e.payload ->> 'product_description', e.payload ->> 'variant',
           (e.payload ->> 'price_amount')::numeric, coalesce(e.payload ->> 'price_currency', 'INR'),
           e.payload ->> 'tax_note', e.payload ->> 'uom', (e.payload ->> 'weight')::numeric,
           e.payload ->> 'weight_unit', (e.payload ->> 'sub_id')::bigint, nullif(e.payload -> 'crop', 'null'::jsonb)
    returning product_id into new_pid;
    if e.payload ? 'tags' then
      perform rvx_set_tags(new_pid, e.payload -> 'tags');
    end if;
    perform rvx_log(cid, lvl, 'add', new_pid, e.page, jsonb_build_object('after', e.payload));
    n_add := n_add + 1;
  end loop;

  delete from review_edit where catalog_id = cid and level = lvl;
  delete from review_page_seen where catalog_id = cid and level = lvl;
  if lvl = 1 then
    update review set level_1_at = now() where catalog_id = cid;
  else
    update review set level_2_at = now() where catalog_id = cid;
  end if;
  perform rvx_log(cid, lvl, 'finish', null, null,
                  jsonb_build_object('updated', n_upd, 'added', n_add, 'removed', n_rem));
  return jsonb_build_object('level', lvl, 'updated', n_upd, 'added', n_add, 'removed', n_rem);
end;
$$;

-- Give up a level. The person who opened it, or an admin, may do this; its drafts are discarded.
create function rv_release(cid bigint, lvl int) returns void
language plpgsql security definer set search_path from current as $$
declare u app_user; r review; holder uuid; done timestamptz; dropped int;
begin
  u := rvx_actor();
  if lvl is null or lvl not in (1, 2) then raise exception 'level must be 1 or 2'; end if;
  select * into r from review where catalog_id = cid for update;
  holder := case lvl when 1 then r.reviewer_1_id else r.reviewer_2_id end;
  done := case lvl when 1 then r.level_1_at else r.level_2_at end;
  if not found or holder is null then raise exception 'level % of this catalogue is not open', lvl; end if;
  if done is not null then raise exception 'level % of this catalogue is already finished', lvl; end if;
  if holder <> u.user_id and u.role <> 'admin' then
    raise exception 'only the reviewer who opened it, or an admin, can release this level' using errcode = '42501';
  end if;
  select count(*) into dropped from review_edit where catalog_id = cid and level = lvl;
  delete from review_edit where catalog_id = cid and level = lvl;
  delete from review_page_seen where catalog_id = cid and level = lvl;
  if lvl = 1 then
    update review set reviewer_1_id = null where catalog_id = cid;
  else
    update review set reviewer_2_id = null where catalog_id = cid;
  end if;
  perform rvx_log(cid, lvl, 'release', null, null,
                  jsonb_build_object('held_by', holder, 'discarded_edits', dropped));
end;
$$;

-- ---------------------------------------------------------------------------------------------
-- Admin functions.

create function rv_audit(cid bigint) returns jsonb
language plpgsql stable security definer set search_path from current as $$
begin
  perform rvx_admin();
  return coalesce((select jsonb_agg(jsonb_build_object(
      'log_id', l.log_id, 'at', l.at, 'by', coalesce(a.email, l.user_id::text), 'level', l.level,
      'action', l.action, 'product_id', l.product_id, 'page', l.page, 'detail', l.detail) order by l.log_id)
    from review_log l left join app_user a on a.user_id = l.user_id where l.catalog_id = cid), '[]'::jsonb);
end;
$$;

-- Sign-up is invite-only: invite the person from the Supabase dashboard (Authentication, Users), then
-- give them a role here. Without an app_user row, a signed-in account can do nothing.
create function admin_add_user(user_email text, user_role text) returns jsonb
language plpgsql security definer set search_path from current as $$
declare a app_user; uid uuid; mail text := lower(btrim(coalesce(user_email, '')));
begin
  a := rvx_admin();
  if user_role not in ('reviewer', 'admin') then raise exception 'role must be reviewer or admin'; end if;
  select id into uid from auth.users where lower(email) = mail order by id limit 1;
  if uid is null then
    raise exception 'no sign-in account for %: invite them from the Supabase dashboard first', mail;
  end if;
  if exists (select 1 from app_user where user_id = uid) then
    raise exception '% is already a user', mail;
  end if;
  insert into app_user (user_id, email, role) values (uid, mail, user_role);
  perform rvx_log(null, null, 'add_user', null, null, jsonb_build_object('user_id', uid, 'email', mail, 'role', user_role));
  return jsonb_build_object('user_id', uid, 'email', mail, 'role', user_role, 'active', true);
end;
$$;

create function admin_set_user(uid uuid, new_role text, is_active boolean) returns jsonb
language plpgsql security definer set search_path from current as $$
declare a app_user; t app_user;
begin
  a := rvx_admin();
  if new_role not in ('reviewer', 'admin') then raise exception 'role must be reviewer or admin'; end if;
  if is_active is null then raise exception 'active must be true or false'; end if;
  select * into t from app_user where user_id = uid for update;
  if not found then raise exception 'unknown user'; end if;
  if t.role = 'admin' and t.active and (new_role <> 'admin' or not is_active)
     and not exists (select 1 from app_user where role = 'admin' and active and user_id <> uid) then
    raise exception 'there must always be one active admin';
  end if;
  update app_user set role = new_role, active = is_active where user_id = uid;
  perform rvx_log(null, null, 'set_user', null, null, jsonb_build_object(
    'user_id', uid, 'before', jsonb_build_object('role', t.role, 'active', t.active),
    'after', jsonb_build_object('role', new_role, 'active', is_active)));
  return jsonb_build_object('user_id', uid, 'email', t.email, 'role', new_role, 'active', is_active);
end;
$$;

create function admin_list_users() returns jsonb
language plpgsql stable security definer set search_path from current as $$
begin
  perform rvx_admin();
  return coalesce((select jsonb_agg(jsonb_build_object('user_id', user_id, 'email', email, 'role', role,
                                                       'active', active) order by email) from app_user), '[]'::jsonb);
end;
$$;

-- Used by the storage policy below.
create function is_active_user() returns boolean
language sql stable security definer set search_path from current as $$
  select exists (select 1 from app_user where user_id = auth.uid() and active)
$$;

-- ---------------------------------------------------------------------------------------------
-- Who may call what. Postgres lets everyone call new functions, and Supabase also lets anon, so take
-- that away from all of them and give the public ones to signed-in users only.
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig, p.proname
           from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = current_schema()
             and (p.proname like 'rv\_%' or p.proname like 'rvx\_%' or p.proname like 'admin\_%'
                  or p.proname = 'is_active_user') loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
    if f.proname like 'rv\_%' or f.proname like 'admin\_%' or f.proname = 'is_active_user' then
      execute format('grant execute on function %s to authenticated', f.sig);
    end if;
  end loop;
end
$$;

-- Reviewers read catalogue PDFs straight from the private bucket. Only on Supabase, which has storage.objects.
do $$
begin
  if to_regclass('storage.objects') is not null then
    execute format(
      'create policy %I on storage.objects for select to authenticated using (bucket_id = ''catalogs'' and is_active_user())',
      'pim_read_catalogs_' || current_schema());
  end if;
end
$$;
