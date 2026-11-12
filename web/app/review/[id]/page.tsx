"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { supabase, type Crop, type Lists, type PageData, type Product, type Progress } from "@/lib/supabase";
import ProductForm from "./ProductForm";

export default function ReviewPage() {
  return (
    <Suspense fallback={<main className="empty">Opening…</main>}>
      <Review />
    </Suspense>
  );
}

const keyOf = (p: Product) => (p.product_id !== null ? `p${p.product_id}` : `n${p.edit_id}`);
const firstLive = (products: Product[]) => products.find((p) => !p.removed);

// Every database call goes through here so a refusal becomes a message the reviewer can read.
async function call<T = unknown>(fn: string, args: Record<string, unknown> = {}): Promise<T> {
  const { data, error } = await supabase.rpc(fn, args);
  if (error) throw new Error(error.message);
  return data as T;
}

function Review() {
  const cid = Number(useParams<{ id: string }>().id);
  const router = useRouter();
  const [progress, setProgress] = useState<Progress | null>(null);
  const [lists, setLists] = useState<Lists | null>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pageNo, setPageNo] = useState(1);
  const [page, setPage] = useState<PageData | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [cropping, setCropping] = useState(false);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [catalogCategory, setCatalogCategory] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [zoom, setZoom] = useState(1);
  const [stageBox, setStageBox] = useState({ w: 0, h: 0 });
  const [aspect, setAspect] = useState(0);
  const stage = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const sheet = useRef<HTMLDivElement>(null);
  const dirty = useRef(false);
  const onDirty = useCallback((d: boolean) => { dirty.current = d; }, []);

  const level = progress?.level ?? 0;
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e));

  // Ask before leaving a product that has typed-in changes nobody saved.
  const guard = (go: () => void) => {
    if (dirty.current && !window.confirm("This product has unsaved changes. Discard them?")) return;
    dirty.current = false;
    go();
  };

  // Track the stage size so zoom 100% means "the whole page fits".
  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setStageBox({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, [progress, lists]);

  const refreshProgress = useCallback(async (lvl: number) => {
    setProgress(await call<Progress>("rv_progress", { cid, lvl }));
  }, [cid]);
  const refreshLists = useCallback(async () => setLists(await call<Lists>("rv_lists")), []);

  // Open the catalogue (claims or resumes the level) and load its PDF from the private bucket.
  useEffect(() => {
    (async () => {
      const p = await call<Progress>("rv_open", { cid });
      const link = await supabase.storage.from("catalogs").createSignedUrl(p.storage_path, 3600);
      if (link.error) throw new Error(`Could not open the PDF: ${link.error.message}`);
      const pdfjs = await import("pdfjs-dist");
      pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
      setPdf(await pdfjs.getDocument({ url: link.data.signedUrl }).promise);
      await refreshLists();
      setProgress(p);
      setPageNo(p.resume_page);
    })().catch(fail);
  }, [cid, refreshLists]);

  // Show the page: fetch its products (this records the page as seen) and draw the PDF page.
  useEffect(() => {
    if (!level || !pdf) return;
    let cancelled = false;
    (async () => {
      const got = await call<PageData>("rv_get_page", { cid, lvl: level, pg: pageNo });
      if (cancelled) return;
      setPage(got);
      setSelected(firstLive(got.products) ? keyOf(firstLive(got.products)!) : null);
      setCropping(false);
      refreshProgress(level).catch(fail);
      const pdfPage = await pdf.getPage(pageNo);
      const viewport = pdfPage.getViewport({ scale: 2 });
      const el = canvas.current;
      if (!el || cancelled) return;
      el.width = viewport.width;
      el.height = viewport.height;
      setAspect(viewport.width / viewport.height);
      await pdfPage.render({ canvas: el, viewport }).promise;
    })().catch((e) => !cancelled && fail(e));
    return () => { cancelled = true; };
  }, [level, pdf, pageNo, cid, refreshProgress]);

  const replace = (doc: Product) =>
    setPage((pg) => pg && { ...pg, products: pg.products.map((p) => (keyOf(p) === keyOf(doc) ? doc : p)) });

  async function save(p: Product, fields: Record<string, unknown>) {
    const doc = await call<Product>("rv_save_product", { cid, lvl: level, pid: p.product_id, eid: p.edit_id, fields });
    replace(doc);
    await refreshProgress(level);
  }

  async function remove(p: Product) {
    await call("rv_remove_product", { cid, lvl: level, pid: p.product_id, eid: p.edit_id });
    dirty.current = false;
    setPage((pg) => {
      if (!pg) return pg;
      const products = p.product_id === null
        ? pg.products.filter((x) => keyOf(x) !== keyOf(p))
        : pg.products.map((x) => (keyOf(x) === keyOf(p) ? { ...x, removed: true } : x));
      return { ...pg, products };
    });
    await refreshProgress(level);
  }

  async function restore(p: Product) {
    await call("rv_restore_product", { cid, lvl: level, pid: p.product_id });
    setPage((pg) => pg && { ...pg, products: pg.products.map((x) => (keyOf(x) === keyOf(p) ? { ...x, removed: false } : x)) });
    await refreshProgress(level);
  }

  async function addProduct() {
    setError("");
    try {
      const doc = await call<Product>("rv_add_product", { cid, lvl: level, pg: pageNo, fields: {} });
      setPage((pg) => pg && { ...pg, products: [...pg.products, doc] });
      setSelected(keyOf(doc));
      await refreshProgress(level);
    } catch (e) { fail(e); }
  }

  const setCrop = (p: Product, crop: Crop) => save(p, { crop });

  // A tag typed by the reviewer joins the pick list at once; the database creates it when the level is finished.
  const addTag = (name: string) =>
    setLists((l) => l && { ...l, tags: [...new Set([...l.tags, name])].sort() });

  async function addCategory(): Promise<number | null> {
    const name = window.prompt("New category name");
    if (!name) return null;
    const made = await call<{ id: number }>("rv_add_category", { cat_name: name });
    await refreshLists();
    return made.id;
  }

  async function addSub(categoryId: number): Promise<number | null> {
    const name = window.prompt("New subcategory name");
    if (!name) return null;
    const made = await call<{ id: number }>("rv_add_subcategory", { cat_id: categoryId, sub_name: name });
    await refreshLists();
    return made.id;
  }

  async function saveBrand(name: string) {
    setError("");
    try {
      if (name.trim() && name.trim() !== progress?.brand_name) {
        await call("rv_set_brand", { cid, lvl: level, new_name: name });
        await refreshProgress(level);
      }
    } catch (e) { fail(e); }
  }

  async function finish() {
    if (!window.confirm(`Finish Level ${level}? This applies all your changes to the catalogue.`)) return;
    setError("");
    try {
      await call("rv_finish", { cid, lvl: level });
      router.push("/");
    } catch (e) { fail(e); }
  }

  // Saved products are already kept on the server, so leaving keeps the draft; "Resume" picks it up again.
  const saveDraft = () => guard(() => router.push("/"));

  // Drawing a crop: positions are kept as fractions of the page.
  const frac = (e: React.PointerEvent) => {
    const r = sheet.current!.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    };
  };
  const rect = drag && {
    x: Math.min(drag.x0, drag.x1), y: Math.min(drag.y0, drag.y1),
    w: Math.abs(drag.x1 - drag.x0), h: Math.abs(drag.y1 - drag.y0),
  };

  async function endDrag() {
    const target = page?.products.find((p) => keyOf(p) === selected);
    const r = rect;
    setDrag(null);
    setCropping(false);
    if (!target || !r || r.w < 0.01 || r.h < 0.01) return;
    try { await setCrop(target, r); } catch (e) { fail(e); }
  }

  const pad = 24;
  const fitW = aspect && stageBox.w ? Math.min(stageBox.w - pad, (stageBox.h - pad) * aspect) : 0;
  const zoomIn = () => setZoom((z) => Math.min(4, Math.round((z + 0.25) * 100) / 100));
  const zoomOut = () => setZoom((z) => Math.max(0.5, Math.round((z - 0.25) * 100) / 100));

  if (error && !progress) return <main className="page"><p className="alert" role="alert">{error}</p><Link href="/">Back to catalogues</Link></main>;
  if (!progress || !lists) return <main className="empty" style={{ minHeight: "100dvh" }}>Opening…</main>;

  const unseen = progress.page_count - progress.pages_seen;
  const live = page?.products.filter((p) => !p.removed) ?? [];
  const current = page?.products.find((p) => keyOf(p) === selected);
  const before = progress.products_per_page.slice(0, pageNo - 1).reduce((a, b) => a + b, 0);
  const idx = current ? live.findIndex((p) => keyOf(p) === selected) : -1;
  const productLabel = idx >= 0
    ? `${before + idx + 1}`
    : live.length ? `${before + 1}–${before + live.length}` : "–";

  return (
    <div className="review">
      <datalist id="all-tags">{lists.tags.map((t) => <option key={t} value={t} />)}</datalist>
      <header className="topbar">
        <Link href="/" onClick={(e) => { if (dirty.current && !window.confirm("This product has unsaved changes. Leave anyway?")) e.preventDefault(); }}>← Catalogues</Link>
        <div>
          <h1>{progress.catalog_name}</h1>
        </div>
        <span className="badge info">Level {progress.level}{progress.level === 2 ? " · second pass" : ""}</span>
        <div className="group">
          <button className="btn small" disabled={pageNo <= 1} onClick={() => guard(() => setPageNo(pageNo - 1))}>‹</button>
          <span className="count">Page <b>{pageNo}</b> / {progress.page_count}</span>
          <button className="btn small" disabled={pageNo >= progress.page_count} onClick={() => guard(() => setPageNo(pageNo + 1))}>›</button>
        </div>
        <div className="group">
          <button className="btn small" onClick={zoomOut} disabled={zoom <= 0.5} aria-label="Zoom out">−</button>
          <button className="btn small" onClick={() => setZoom(1)} title="Fit the whole page" style={{ minWidth: 52 }}>{Math.round(zoom * 100)}%</button>
          <button className="btn small" onClick={zoomIn} disabled={zoom >= 4} aria-label="Zoom in">+</button>
        </div>
        <span className="count">Product <b>{productLabel}</b> / {progress.product_count}</span>
        <span className="spacer" />
        <div className="group">
          <span className="label" style={{ margin: 0 }}>Brand</span>
          <input
            className="input" style={{ width: 150 }} key={progress.brand_name}
            defaultValue={progress.brand_name} onBlur={(e) => saveBrand(e.target.value)}
          />
          <span className="label" style={{ margin: 0 }}>Category</span>
          <select
            className="input" style={{ width: 140 }} value={catalogCategory ?? ""}
            onChange={async (e) => {
              if (e.target.value === "new") {
                try { setCatalogCategory(await addCategory()); } catch (err) { fail(err); }
              } else setCatalogCategory(e.target.value === "" ? null : Number(e.target.value));
            }}
          >
            <option value="">—</option>
            {lists.categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            <option value="new">+ New category…</option>
          </select>
        </div>
        <span className="muted count" title={unseen > 0 ? `${unseen} page(s) still to view` : ""}>
          {progress.pages_seen}/{progress.page_count} viewed
        </span>
        <button className="btn" onClick={saveDraft} title="Your saved changes are kept. Resume later from the catalogue list.">Save draft</button>
        <button className="btn primary" onClick={() => guard(finish)} disabled={!progress.can_finish} title={progress.can_finish ? "" : `${unseen} page(s) still to view`}>
          Finish Level {progress.level}
        </button>
      </header>
      {error && <p className="alert" role="alert">{error} <button className="btn small" onClick={() => setError("")}>Dismiss</button></p>}

      <div className="body">
        <div className="stage" ref={stage}>
          <div className="sheet" ref={sheet}>
            <canvas ref={canvas} style={fitW ? { width: fitW * zoom, maxWidth: "none", maxHeight: "none" } : undefined} />
            {live.map((p) => {
              if (!p.crop || "none" in p.crop) return null;
              return (
                <div key={keyOf(p)} className={`crop-box${keyOf(p) === selected ? " on" : ""}`} style={{
                  left: `${p.crop.x * 100}%`, top: `${p.crop.y * 100}%`, width: `${p.crop.w * 100}%`, height: `${p.crop.h * 100}%`,
                }} />
              );
            })}
            {cropping && (
              <div
                className="crop-draw"
                onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); const f = frac(e); setDrag({ x0: f.x, y0: f.y, x1: f.x, y1: f.y }); }}
                onPointerMove={(e) => { if (drag) { const f = frac(e); setDrag({ ...drag, x1: f.x, y1: f.y }); } }}
                onPointerUp={endDrag}
              >
                {rect && <div style={{
                  position: "absolute", left: `${rect.x * 100}%`, top: `${rect.y * 100}%`,
                  width: `${rect.w * 100}%`, height: `${rect.h * 100}%`, border: "2px solid #dc2626", background: "rgba(220,38,38,.15)",
                }} />}
              </div>
            )}
          </div>
        </div>

        <aside className="panel">
          <div className="card plist">
            <div className="head">
              <span>
                <b>Products on this page</b>{" "}
                {page && page.page_type !== "product" && <span className="badge">{page.failed ? "extraction failed" : "non-product page"}</span>}
              </span>
              <span className="group">
                <button className="btn small" onClick={addProduct} disabled={!page}>+ Add product</button>
                <button
                  className="btn small danger" disabled={!current || current.removed}
                  onClick={async () => { if (current) { try { await remove(current); } catch (e) { fail(e); } } }}
                >
                  {current && current.product_id === null ? "Discard product" : "Remove product"}
                </button>
              </span>
            </div>
            {page?.products.map((p, i) => {
              const bad = !p.removed && (!p.product_name || p.price_amount === null);
              return (
                <button
                  key={keyOf(p)}
                  className={`prow${keyOf(p) === selected ? " sel" : ""}${p.removed ? " gone" : ""}`}
                  onClick={() => { if (selected !== keyOf(p)) guard(() => { setSelected(keyOf(p)); setCropping(false); }); }}
                >
                  <span className="no">{i + 1}</span>
                  <span className="nm">{p.product_name ?? "(no name)"}{p.variant ? ` · ${p.variant}` : ""}</span>
                  <span>{p.price_amount === null ? "–" : `${p.price_currency} ${p.price_amount}`}</span>
                  {p.removed ? <span className="badge">removed</span>
                    : bad ? <span className="badge warn">needs info</span>
                    : p.edited ? <span className="badge ok">edited</span> : <span />}
                </button>
              );
            })}
            {page && page.products.length === 0 && (
              <div style={{ padding: 12, color: "var(--muted)" }}>No products on this page. Use “Add product” if there should be one.</div>
            )}
          </div>

          <div className="card" style={{ display: "flex", flex: 1, minHeight: 0, flexDirection: "column" }}>
            {current && lists ? (
              <ProductForm
                key={`${keyOf(current)}-${catalogCategory}`}
                product={current}
                lists={lists}
                defaultCategory={catalogCategory}
                cropping={cropping}
                onDirty={onDirty}
                onSave={(f) => save(current, f)}
                onRestore={() => restore(current)}
                onCrop={async (mode) => {
                  if (mode === "draw") setCropping(true);
                  else { setCropping(false); await setCrop(current, mode === "whole" ? null : { none: true }); }
                }}
                onAddCategory={addCategory}
                onAddSub={addSub}
                onNewTag={addTag}
              />
            ) : (
              <div className="empty">Select a product from the list to edit it.</div>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
