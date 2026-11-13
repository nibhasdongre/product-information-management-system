-- Storefront backend: customers, wishlist, and the read-only functions the storefront calls.
--
-- Customers are deliberately NOT rows of app_user. app_user means "reviewer or admin", and every review
-- function and the PDF storage policy let any active app_user in. A customer lives in `customer`, so
-- they can call the sf_* functions and nothing else. Reviewers and admins may also use the storefront
-- (to preview it), because sfx_actor accepts either.
--
-- A product is visible in the storefront once its catalogue has finished Level 2 and it has a price.
-- Browsers never read tables: the tables have RLS with no policies, and sf_* functions are the only way in.

create table customer (
  user_id     uuid primary key references auth.users (id) on delete cascade,
  email       text not null,
  company     text,
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

create table wishlist_item (
  user_id        uuid not null references auth.users (id) on delete cascade,
  product_id     bigint not null references product (product_id) on delete cascade,
  quantity       int check (quantity is null or quantity between 1 and 1000000),
  customization  text check (customization is null or length(customization) <= 1000),
  added_at       timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  primary key (user_id, product_id)
);
create index wishlist_item_product_idx on wishlist_item (product_id);

alter table customer      enable row level security;
alter table wishlist_item enable row level security;

-- ---------------------------------------------------------------------------------------------
-- Internal helpers (sfx_*). Not callable from a browser.

-- The caller's id, if they are an active customer or an active reviewer/admin.
create function sfx_actor() returns uuid
language plpgsql stable security definer set search_path from current as $$
begin
  if auth.uid() is null
     or not (exists (select 1 from customer where user_id = auth.uid() and active)
             or exists (select 1 from app_user where user_id = auth.uid() and active)) then
    raise exception 'not an active user' using errcode = '42501';
  end if;
  return auth.uid();
end;
$$;

-- Every product the storefront may show, with its brand, category and tags. Owner rights, so it sees
-- the tables; nobody else is granted it (see the grants at the end).
create view sfx_product as
  select p.product_id, x.catalog_id, x.page, p.position as pos,
         b.brand_id, b.brand_name, c.category_id, c.category_name, p.sub_id, s.subcat_name,
         p.product_name, p.product_description, p.variant, p.price_amount, p.price_currency, p.tax_note,
         p.uom, p.weight, p.weight_unit, p.cropped_path, t.tags,
         lower(concat_ws(' ', p.product_name, p.product_description, p.variant, b.brand_name,
                         c.category_name, s.subcat_name, array_to_string(t.tags, ' '))) as haystack
  from product p
  join page_extractions x on x.extraction_id = p.extraction_id
  join review r on r.catalog_id = x.catalog_id and r.level_2_at is not null
  join catalog ca on ca.catalog_id = x.catalog_id
  join brand b on b.brand_id = ca.brand_id
  left join subcategory s on s.sub_id = p.sub_id
  left join category c on c.category_id = s.category_id
  cross join lateral (select coalesce(array_agg(tg.tag_name order by tg.tag_name), '{}'::text[]) as tags
                      from product_tag pt join tag tg using (tag_id) where pt.product_id = p.product_id) t
  where x.extraction_id = rvx_page_extraction(x.catalog_id, x.page)
    and p.price_amount is not null;

-- One product as the storefront shows it. `detailed` adds the fields only the detail view needs.
-- image_path is the published image folder; the page appends /card.webp or /detail.webp.
create function sfx_card(p sfx_product, listed boolean, w wishlist_item, detailed boolean) returns jsonb
language sql stable security definer set search_path from current as $$
  select jsonb_build_object(
    'product_id', p.product_id, 'product_name', p.product_name, 'variant', p.variant,
    'price_amount', p.price_amount, 'price_currency', p.price_currency,
    'brand_id', p.brand_id, 'brand_name', p.brand_name,
    'category_id', p.category_id, 'category_name', p.category_name,
    'sub_id', p.sub_id, 'subcat_name', p.subcat_name,
    'tags', to_jsonb(p.tags), 'image_path', p.cropped_path, 'in_wishlist', listed)
  || case when detailed then jsonb_build_object(
       'product_description', p.product_description, 'tax_note', p.tax_note, 'uom', p.uom,
       'weight', p.weight, 'weight_unit', p.weight_unit) else '{}'::jsonb end
  || case when w.user_id is not null then jsonb_build_object(
       'quantity', w.quantity, 'customization', w.customization, 'added_at', w.added_at) else '{}'::jsonb end
$$;

-- ---------------------------------------------------------------------------------------------
-- Functions the storefront calls.

create function sf_me() returns jsonb
language plpgsql stable security definer set search_path from current as $$
declare uid uuid := sfx_actor(); c customer; a app_user; n int;
begin
  select count(*) into n from wishlist_item w join sfx_product p using (product_id) where w.user_id = uid;
  select * into c from customer where user_id = uid and active;
  if found then
    return jsonb_build_object('user_id', uid, 'email', c.email, 'company', c.company, 'kind', 'customer', 'wishlist_count', n);
  end if;
  select * into a from app_user where user_id = uid;
  return jsonb_build_object('user_id', uid, 'email', a.email, 'company', null, 'kind', 'staff', 'role', a.role, 'wishlist_count', n);
end;
$$;

-- What the filter panel offers: brands, categories with their subcategories, tags and the price range,
-- each with the number of products it would show.
create function sf_filters() returns jsonb
language plpgsql stable security definer set search_path from current as $$
begin
  perform sfx_actor();
  return jsonb_build_object(
    'total', (select count(*) from sfx_product),
    'price_min', (select min(price_amount) from sfx_product),
    'price_max', (select max(price_amount) from sfx_product),
    'brands', coalesce((select jsonb_agg(jsonb_build_object('id', brand_id, 'name', brand_name, 'count', n) order by brand_name)
                        from (select brand_id, brand_name, count(*) n from sfx_product group by 1, 2) q), '[]'::jsonb),
    'categories', coalesce((
      select jsonb_agg(jsonb_build_object('id', c.category_id, 'name', c.category_name, 'count', c.n,
               'subcategories', (select coalesce(jsonb_agg(jsonb_build_object('id', s.sub_id, 'name', s.subcat_name, 'count', s.n)
                                                          order by s.subcat_name), '[]'::jsonb)
                                 from (select sub_id, subcat_name, count(*) n from sfx_product
                                       where category_id = c.category_id group by 1, 2) s)) order by c.category_name)
      from (select category_id, category_name, count(*) n from sfx_product where category_id is not null group by 1, 2) c),
      '[]'::jsonb),
    'tags', coalesce((select jsonb_agg(jsonb_build_object('name', tag, 'count', n) order by tag)
                      from (select unnest(tags) tag, count(*) n from sfx_product group by 1) q), '[]'::jsonb));
end;
$$;

-- Search and filter. Words in search_text must each appear somewhere in the product's name, description,
-- variant, brand, category, subcategory or tags. Within one filter any value matches; across filters all must.
create function sf_search(search_text text default null, brand_ids bigint[] default null,
                          category_ids bigint[] default null, sub_ids bigint[] default null,
                          tag_names text[] default null, price_min numeric default null,
                          price_max numeric default null, sort_by text default 'featured',
                          page_no int default 1, page_size int default 24) returns jsonb
language plpgsql stable security definer set search_path from current as $$
declare
  uid uuid := sfx_actor();
  terms text[]; wanted_tags text[]; offs int; result jsonb;
begin
  search_text := btrim(coalesce(search_text, ''));
  if length(search_text) > 200 then raise exception 'search text is too long (200 characters at most)'; end if;
  if coalesce(cardinality(brand_ids), 0) > 100 or coalesce(cardinality(category_ids), 0) > 100
     or coalesce(cardinality(sub_ids), 0) > 100 or coalesce(cardinality(tag_names), 0) > 100 then
    raise exception 'too many filter values (100 at most per filter)';
  end if;
  if sort_by is null or sort_by not in ('featured', 'price_asc', 'price_desc', 'name') then
    raise exception 'sort_by must be featured, price_asc, price_desc or name';
  end if;
  if page_no is null or page_no < 1 then raise exception 'page_no must be 1 or more'; end if;
  if page_size is null or page_size < 1 or page_size > 100 then raise exception 'page_size must be between 1 and 100'; end if;
  if price_min is not null and price_max is not null and price_min > price_max then
    raise exception 'price_min cannot be more than price_max';
  end if;
  terms := array(select x from unnest(regexp_split_to_array(lower(search_text), '\s+')) x where x <> '');
  if cardinality(terms) > 10 then raise exception 'too many search words (10 at most)'; end if;
  wanted_tags := array(select lower(btrim(x)) from unnest(coalesce(tag_names, '{}')) x where btrim(x) <> '');
  offs := (page_no - 1) * page_size;

  with hit as (
    select p.product_id,
           row_number() over (order by
             case when sort_by = 'price_asc' then p.price_amount end asc,
             case when sort_by = 'price_desc' then p.price_amount end desc,
             case when sort_by = 'name' then lower(p.product_name) end asc,
             p.catalog_id, p.page, p.pos, p.product_id) as rn
    from sfx_product p
    where (coalesce(cardinality(brand_ids), 0) = 0 or p.brand_id = any (brand_ids))
      and (coalesce(cardinality(category_ids), 0) = 0 or p.category_id = any (category_ids))
      and (coalesce(cardinality(sub_ids), 0) = 0 or p.sub_id = any (sub_ids))
      and (cardinality(wanted_tags) = 0 or p.tags && wanted_tags)
      and (price_min is null or p.price_amount >= price_min)
      and (price_max is null or p.price_amount <= price_max)
      and not exists (select 1 from unnest(terms) t
                      where p.haystack not like '%' || replace(replace(replace(t, '\', '\\'), '%', '\%'), '_', '\_') || '%')
  )
  select jsonb_build_object(
           'total', (select count(*) from hit), 'page', page_no, 'page_size', page_size,
           'items', coalesce((select jsonb_agg(sfx_card(p, w.user_id is not null, null::wishlist_item, false) order by h.rn)
                              from hit h join sfx_product p using (product_id)
                              left join wishlist_item w on w.user_id = uid and w.product_id = p.product_id
                              where h.rn > offs and h.rn <= offs + page_size), '[]'::jsonb))
    into result;
  return result;
end;
$$;

create function sf_get_product(pid bigint) returns jsonb
language plpgsql stable security definer set search_path from current as $$
declare uid uuid := sfx_actor(); p sfx_product; w wishlist_item;
begin
  select * into p from sfx_product where product_id = pid;
  if not found then raise exception 'product not found'; end if;
  select * into w from wishlist_item where user_id = uid and product_id = pid;
  return sfx_card(p, w.user_id is not null, w, true);
end;
$$;

-- ---- wishlist --------------------------------------------------------------------------------

create function sf_wishlist() returns jsonb
language plpgsql stable security definer set search_path from current as $$
declare uid uuid := sfx_actor();
begin
  return jsonb_build_object('items', coalesce((
    select jsonb_agg(sfx_card(p, true, w, true) order by w.added_at, w.product_id)
    from wishlist_item w join sfx_product p using (product_id) where w.user_id = uid), '[]'::jsonb));
end;
$$;

create function sf_wishlist_add(pid bigint) returns jsonb
language plpgsql security definer set search_path from current as $$
declare uid uuid := sfx_actor();
begin
  if not exists (select 1 from sfx_product where product_id = pid) then raise exception 'product not found'; end if;
  insert into wishlist_item (user_id, product_id) values (uid, pid) on conflict do nothing;
  return jsonb_build_object('count', (select count(*) from wishlist_item w join sfx_product p using (product_id) where w.user_id = uid));
end;
$$;

create function sf_wishlist_remove(pid bigint) returns jsonb
language plpgsql security definer set search_path from current as $$
declare uid uuid := sfx_actor();
begin
  delete from wishlist_item where user_id = uid and product_id = pid;
  return jsonb_build_object('count', (select count(*) from wishlist_item w join sfx_product p using (product_id) where w.user_id = uid));
end;
$$;

-- Set the estimated quantity (1 to 1,000,000, or null for none) and the customization note of one item.
create function sf_wishlist_update(pid bigint, qty int, note text) returns jsonb
language plpgsql security definer set search_path from current as $$
declare uid uuid := sfx_actor(); s text := nullif(btrim(coalesce(note, '')), ''); w wishlist_item; p sfx_product;
begin
  if qty is not null and (qty < 1 or qty > 1000000) then raise exception 'quantity must be between 1 and 1000000'; end if;
  if length(coalesce(s, '')) > 1000 then raise exception 'the note is too long (1000 characters at most)'; end if;
  update wishlist_item set quantity = qty, customization = s, updated_at = now()
    where user_id = uid and product_id = pid returning * into w;
  if not found then raise exception 'that product is not in your wishlist'; end if;
  select * into p from sfx_product where product_id = pid;
  return sfx_card(p, true, w, true);
end;
$$;

-- ---------------------------------------------------------------------------------------------
-- Who may call what: sf_* to signed-in users only; sfx_* and the view to nobody.
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig, p.proname
           from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = current_schema() and (p.proname like 'sf\_%' or p.proname like 'sfx\_%') loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
    if f.proname like 'sf\_%' then
      execute format('grant execute on function %s to authenticated', f.sig);
    end if;
  end loop;
end
$$;
revoke all on sfx_product from public, anon, authenticated;
