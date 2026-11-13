-- Categories and subcategories now combine as "either": picking the Kitchen category and the Stationery subcategory
-- shows Kitchen products and Stationery products. (Before, a product had to match both.)

-- Search and filter. Words in search_text must each appear somewhere in the product's name, description,
-- variant, brand, category, subcategory or tags. Within one filter any value matches; across filters all must. Categories and subcategories count as one filter: a product matches if its category or its subcategory was picked.
create or replace function sf_search(search_text text default null, brand_ids bigint[] default null,
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
      and (coalesce(cardinality(category_ids), 0) + coalesce(cardinality(sub_ids), 0) = 0
           or p.category_id = any (category_ids) or p.sub_id = any (sub_ids))
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
