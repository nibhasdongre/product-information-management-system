"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Gate from "@/components/Gate";
import { CloseIcon, SearchIcon } from "@/components/Icons";
import ProductCard from "@/components/ProductCard";
import ProductDrawer from "@/components/ProductDrawer";
import SiteHeader from "@/components/SiteHeader";
import { useApp } from "@/lib/app";
import { call, money, type Filters, type SearchResult } from "@/lib/supabase";

const PAGE_SIZE = 24;
const SORTS = [
  ["featured", "Featured"],
  ["price_asc", "Price, low to high"],
  ["price_desc", "Price, high to low"],
  ["name", "Name, A to Z"],
] as const;

const nums = (v: string | null) => (v ? v.split(",").map(Number).filter((n) => Number.isFinite(n)) : []);
const strs = (v: string | null) => (v ? v.split(",").filter(Boolean) : []);
const numOrNull = (v: string | null) => (v && Number.isFinite(Number(v)) ? Number(v) : null);

export default function CataloguePage() {
  return (
    <>
      <SiteHeader />
      <Suspense fallback={<p className="empty">Opening…</p>}>
        <Gate><Catalogue /></Gate>
      </Suspense>
    </>
  );
}

function Catalogue() {
  const router = useRouter();
  const params = useSearchParams();
  const { wishlistIds, toggleWishlist } = useApp();

  // Everything the person has chosen lives in the address, so a filtered view can be shared or bookmarked.
  const f = useMemo(() => ({
    q: params.get("q") ?? "",
    cats: nums(params.get("cat")), subs: nums(params.get("sub")), brands: nums(params.get("brand")),
    tags: strs(params.get("tag")), min: numOrNull(params.get("min")), max: numOrNull(params.get("max")),
    sort: SORTS.some(([k]) => k === params.get("sort")) ? (params.get("sort") as string) : "featured",
  }), [params]);
  const productId = numOrNull(params.get("product"));

  const set = useCallback((patch: Record<string, string | number | number[] | string[] | null>) => {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(patch)) {
      const text = Array.isArray(v) ? v.join(",") : v === null ? "" : String(v);
      if (text === "") next.delete(k);
      else next.set(k, text);
    }
    const qs = next.toString();
    router.replace(qs ? `/catalogue?${qs}` : "/catalogue", { scroll: false });
  }, [params, router]);

  const [filters, setFilters] = useState<Filters | null>(null);
  const [result, setResult] = useState<{ key: string; total: number; items: SearchResult["items"]; page: number } | null>(null);
  const [error, setError] = useState("");
  const [more, setMore] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [text, setText] = useState(f.q);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const key = useMemo(() => {
    const p = new URLSearchParams(params.toString());
    p.delete("product");
    return p.toString();
  }, [params]);

  useEffect(() => {
    call<Filters>("sf_filters").then(setFilters).catch((e) => setError(String(e.message ?? e)));
  }, []);

  const args = useCallback((page: number) => ({
    search_text: f.q, brand_ids: f.brands, category_ids: f.cats, sub_ids: f.subs, tag_names: f.tags,
    price_min: f.min, price_max: f.max, sort_by: f.sort, page_no: page, page_size: PAGE_SIZE,
  }), [f]);

  useEffect(() => {
    let cancelled = false;
    call<SearchResult>("sf_search", args(1))
      .then((r) => { if (!cancelled) { setResult({ key, total: r.total, items: r.items, page: 1 }); setError(""); } })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [key, args]);

  async function showMore() {
    if (!result) return;
    setMore(true);
    try {
      const r = await call<SearchResult>("sf_search", args(result.page + 1));
      setResult({ ...result, items: [...result.items, ...r.items], total: r.total, page: result.page + 1 });
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    setMore(false);
  }

  const loading = !result || result.key !== key;
  const toggle = (arr: number[], v: number) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);
  const toggleStr = (arr: string[], v: string) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

  function onSearch(v: string) {
    setText(v);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => set({ q: v.trim() || null }), 350);
  }

  async function heart(id: number) {
    try { await toggleWishlist(id); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }

  const catName = (id: number) => filters?.categories.find((c) => c.id === id)?.name ?? "Category";
  const subName = (id: number) => filters?.categories.flatMap((c) => c.subcategories).find((s) => s.id === id)?.name ?? "Subcategory";
  const brandName = (id: number) => filters?.brands.find((b) => b.id === id)?.name ?? "Brand";
  const hasFilters = f.cats.length + f.subs.length + f.brands.length + f.tags.length > 0 || f.min !== null || f.max !== null;

  return (
    <main className="container">
      <div className="shop-bar">
        <div className="search">
          <SearchIcon />
          <input
            className="input" type="search" value={text} onChange={(e) => onSearch(e.target.value)}
            placeholder="Search by product, brand, category or tag" aria-label="Search products"
          />
        </div>
      </div>

      <div className="shop">
        {filtersOpen && <div className="scrim" style={{ zIndex: 40 }} onClick={() => setFiltersOpen(false)} />}
        <aside className={`filters${filtersOpen ? " open" : ""}`} aria-label="Filters">
          <button className="btn line sm filters-close" onClick={() => setFiltersOpen(false)}><CloseIcon size={16} /> Close filters</button>
          {filters && (
            <>
              <div className="fgroup first">
                <h3>Category</h3>
                {filters.categories.length === 0 && <p style={{ color: "var(--muted)", fontSize: 14 }}>No categories yet.</p>}
                {filters.categories.map((c) => {
                  const open = f.cats.includes(c.id) || c.subcategories.some((s) => f.subs.includes(s.id));
                  return (
                    <div key={c.id}>
                      <label className="check">
                        <input type="checkbox" checked={f.cats.includes(c.id)}
                          onChange={() => set({ cat: toggle(f.cats, c.id), sub: f.subs.filter((s) => !c.subcategories.some((x) => x.id === s)) })} />
                        {c.name}<span className="n">{c.count}</span>
                      </label>
                      {open && c.subcategories.map((s) => (
                        <label className="check sub" key={s.id}>
                          <input type="checkbox" checked={f.subs.includes(s.id)}
                            onChange={() => set({ sub: toggle(f.subs, s.id), cat: f.cats.filter((x) => x !== c.id) })} />
                          {s.name}<span className="n">{s.count}</span>
                        </label>
                      ))}
                    </div>
                  );
                })}
              </div>
              <div className="fgroup">
                <h3>Price</h3>
                <div className="pricebox" key={`${f.min}-${f.max}`}>
                  <input className="input" type="number" min={0} inputMode="numeric" aria-label="Minimum price"
                    placeholder={filters.price_min !== null ? `From ${Math.floor(filters.price_min)}` : "From"} defaultValue={f.min ?? ""}
                    onBlur={(e) => set({ min: e.target.value || null })} onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()} />
                  <input className="input" type="number" min={0} inputMode="numeric" aria-label="Maximum price"
                    placeholder={filters.price_max !== null ? `To ${Math.ceil(filters.price_max)}` : "To"} defaultValue={f.max ?? ""}
                    onBlur={(e) => set({ max: e.target.value || null })} onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()} />
                </div>
              </div>
              <div className="fgroup">
                <h3>Brand</h3>
                {filters.brands.map((b) => (
                  <label className="check" key={b.id}>
                    <input type="checkbox" checked={f.brands.includes(b.id)} onChange={() => set({ brand: toggle(f.brands, b.id) })} />
                    {b.name}<span className="n">{b.count}</span>
                  </label>
                ))}
              </div>
              {filters.tags.length > 0 && (
                <div className="fgroup">
                  <h3>Tags</h3>
                  <div className="chips">
                    {filters.tags.map((t) => (
                      <button key={t.name} className="chip" aria-pressed={f.tags.includes(t.name)} onClick={() => set({ tag: toggleStr(f.tags, t.name) })}>
                        {t.name}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </aside>

        <section aria-live="polite">
          <div className="results-head">
            <button className="btn line sm filter-toggle" onClick={() => setFiltersOpen(true)}>Filters</button>
            <span className="count">
              {loading ? "Searching…" : result.total === 0 ? "No products found" : `${result.total} product${result.total === 1 ? "" : "s"}`}
            </span>
            <label className="sr-only" htmlFor="sort">Sort by</label>
            <select id="sort" className="input" value={f.sort} onChange={(e) => set({ sort: e.target.value === "featured" ? null : e.target.value })}>
              {SORTS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
            </select>
          </div>

          {hasFilters && (
            <div className="active">
              {f.cats.map((id) => <button key={`c${id}`} className="chip" onClick={() => set({ cat: f.cats.filter((x) => x !== id) })}>{catName(id)} <CloseIcon size={14} /></button>)}
              {f.subs.map((id) => <button key={`s${id}`} className="chip" onClick={() => set({ sub: f.subs.filter((x) => x !== id) })}>{subName(id)} <CloseIcon size={14} /></button>)}
              {f.brands.map((id) => <button key={`b${id}`} className="chip" onClick={() => set({ brand: f.brands.filter((x) => x !== id) })}>{brandName(id)} <CloseIcon size={14} /></button>)}
              {f.tags.map((t) => <button key={`t${t}`} className="chip" onClick={() => set({ tag: f.tags.filter((x) => x !== t) })}>{t} <CloseIcon size={14} /></button>)}
              {(f.min !== null || f.max !== null) && (
                <button className="chip" onClick={() => set({ min: null, max: null })}>
                  {f.min !== null ? money(f.min) : "Any"} to {f.max !== null ? money(f.max) : "any"} <CloseIcon size={14} />
                </button>
              )}
              <button className="link" onClick={() => set({ cat: null, sub: null, brand: null, tag: null, min: null, max: null })}>Clear all</button>
            </div>
          )}

          {error && <p className="alert" role="alert">{error}</p>}

          {result && result.items.length > 0 && (
            <div className="grid" style={{ opacity: loading ? 0.5 : 1 }}>
              {result.items.map((c) => (
                <ProductCard key={c.product_id} card={c} listed={wishlistIds.has(c.product_id)}
                  onOpen={() => set({ product: c.product_id })} onToggle={() => void heart(c.product_id)} />
              ))}
            </div>
          )}
          {!loading && result.total === 0 && !error && (
            <div className="empty">
              <h2>Nothing matches yet</h2>
              <p>{filters?.total === 0 ? "The catalogue has no products at the moment." : "Try a different word, or remove a filter."}</p>
              {hasFilters && <button className="btn line" style={{ marginTop: 16 }} onClick={() => { setText(""); router.replace("/catalogue"); }}>Clear search and filters</button>}
            </div>
          )}
          {result && result.items.length < result.total && (
            <div className="more">
              <button className="btn line" onClick={() => void showMore()} disabled={more}>
                {more ? "Loading…" : `Show more (${result.total - result.items.length} left)`}
              </button>
            </div>
          )}
        </section>
      </div>

      {productId !== null && <ProductDrawer id={productId} onClose={() => set({ product: null })} />}
    </main>
  );
}
