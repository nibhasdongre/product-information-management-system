"use client";

import { useEffect, useState } from "react";
import type { Lists, Product } from "@/lib/supabase";

type Props = {
  product: Product;
  lists: Lists;
  defaultCategory: number | null;
  cropping: boolean;
  onDirty: (dirty: boolean) => void;
  onSave: (fields: Record<string, unknown>) => Promise<void>;
  onRestore: () => Promise<void>;
  onCrop: (mode: "draw" | "whole" | "none") => Promise<void>;
  onAddCategory: () => Promise<number | null>;
  onAddSub: (categoryId: number) => Promise<number | null>;
  onNewTag: (name: string) => void;
};

type Draft = {
  product_name: string; product_description: string; variant: string;
  price_amount: string; price_currency: string; tax_note: string;
  uom: string; weight: string; weight_unit: string; tags: string[];
  sub_id: number | null; category: number | null;
};

const s = (v: string | number | null | undefined) => (v === null || v === undefined ? "" : String(v));

function toDraft(p: Product, lists: Lists, fallbackCategory: number | null): Draft {
  return {
    product_name: s(p.product_name), product_description: s(p.product_description), variant: s(p.variant),
    price_amount: s(p.price_amount), price_currency: s(p.price_currency), tax_note: s(p.tax_note),
    uom: s(p.uom), weight: s(p.weight), weight_unit: s(p.weight_unit), tags: p.tags,
    sub_id: p.sub_id,
    category: lists.subcategories.find((x) => x.id === p.sub_id)?.category_id ?? fallbackCategory,
  };
}

// The values the server would store for this draft, in the shapes the server expects.
function toFields(d: Draft): Record<string, unknown> {
  const text = (v: string) => (v.trim() === "" ? null : v.trim());
  const num = (v: string) => (v.trim() === "" ? null : Number(v));
  return {
    product_name: text(d.product_name), product_description: text(d.product_description), variant: text(d.variant),
    price_amount: num(d.price_amount), price_currency: d.price_currency.trim().toUpperCase(), tax_note: text(d.tax_note),
    uom: text(d.uom), weight: num(d.weight), weight_unit: text(d.weight_unit),
    tags: [...new Set(d.tags)].sort(),
    sub_id: d.sub_id,
  };
}

function cropLabel(p: Product) {
  if (p.crop === null) return "whole page";
  if ("none" in p.crop) return "no image";
  return "custom crop";
}

export default function ProductForm(props: Props) {
  const { product: p, lists } = props;
  const [draft, setDraft] = useState<Draft>(() => toDraft(p, lists, props.defaultCategory));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [newTag, setNewTag] = useState("");

  const fields = toFields(draft);
  const changed = Object.fromEntries(
    Object.entries(fields).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify((p as Record<string, unknown>)[k] ?? null)),
  );
  const dirty = Object.keys(changed).length > 0;

  const { onDirty } = props;
  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty, onDirty]);

  async function run(fn: () => Promise<void>): Promise<boolean> {
    setError("");
    setSaved(false);
    setBusy(true);
    try {
      await fn();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const toggleTag = (t: string) =>
    setDraft((d) => ({ ...d, tags: d.tags.includes(t) ? d.tags.filter((x) => x !== t) : [...d.tags, t] }));
  function addNewTag() {
    const t = newTag.trim().toLowerCase();
    if (!t) return;
    if (t.length > 50) return setError("A tag can be at most 50 characters.");
    props.onNewTag(t);
    setDraft((d) => (d.tags.includes(t) ? d : { ...d, tags: [...d.tags, t] }));
    setNewTag("");
  }

  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));
  const input = (k: keyof Draft & string, extra: Record<string, unknown> = {}) => ({
    className: "input", value: draft[k] as string, onChange: (e: React.ChangeEvent<HTMLInputElement>) => set(k, e.target.value as never), ...extra,
  });

  if (p.removed)
    return (
      <div className="empty">
        <div>
          <p><s>{p.product_name ?? "(no name)"}</s> is marked for removal.</p>
          <button className="btn" onClick={() => run(props.onRestore)}>Restore product</button>
          {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
        </div>
      </div>
    );

  const subs = lists.subcategories.filter((x) => x.category_id === draft.category);
  const nameBad = draft.product_name.trim() === "";
  const priceBad = draft.price_amount.trim() === "";

  return (
    <div className="editor">
      <div className="grid">
        <div className="c12">
          <label className="label req">Product name</label>
          <input {...input("product_name", { className: `input${nameBad ? " invalid" : ""}` })} />
        </div>
        <div className="c12">
          <label className="label">Description</label>
          <textarea
            className="input" rows={6} value={draft.product_description}
            onChange={(e) => set("product_description", e.target.value)}
          />
        </div>
        <div className="c4">
          <label className="label">Variant</label>
          <input {...input("variant")} />
        </div>
        <div className="c3">
          <label className="label req">Price amount</label>
          <input {...input("price_amount", { type: "number", step: "any", min: 0, className: `input${priceBad ? " invalid" : ""}` })} />
        </div>
        <div className="c2" style={{ gridColumn: "span 2" }}>
          <label className="label req">Currency</label>
          <input {...input("price_currency")} />
        </div>
        <div className="c3">
          <label className="label">Tax note</label>
          <input {...input("tax_note")} />
        </div>
        <div className="c4">
          <label className="label">UOM</label>
          <input {...input("uom")} />
        </div>
        <div className="c4">
          <label className="label">Weight</label>
          <input {...input("weight", { type: "number", step: "any", min: 0 })} />
        </div>
        <div className="c4">
          <label className="label">Weight unit</label>
          <input {...input("weight_unit")} />
        </div>
        <div className="c4">
          <label className="label">Category</label>
          <select
            className="input"
            value={draft.category ?? ""}
            onChange={async (e) => {
              if (e.target.value === "new") {
                const id = await props.onAddCategory().catch((err) => { setError(String(err.message ?? err)); return null; });
                if (id) setDraft((d) => ({ ...d, category: id, sub_id: null }));
              } else setDraft((d) => ({ ...d, category: e.target.value === "" ? null : Number(e.target.value), sub_id: null }));
            }}
          >
            <option value="">—</option>
            {lists.categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            <option value="new">+ New category…</option>
          </select>
        </div>
        <div className="c4">
          <label className="label">Subcategory</label>
          <select
            className="input"
            value={draft.sub_id ?? ""}
            disabled={draft.category === null}
            onChange={async (e) => {
              if (e.target.value === "new" && draft.category !== null) {
                const id = await props.onAddSub(draft.category).catch((err) => { setError(String(err.message ?? err)); return null; });
                if (id) set("sub_id", id);
              } else set("sub_id", e.target.value === "" ? null : Number(e.target.value));
            }}
          >
            <option value="">—</option>
            {subs.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            <option value="new">+ New subcategory…</option>
          </select>
        </div>
        <div className="c12">
          <label className="label">Tags</label>
          <div className="taglist">
            {draft.tags.length === 0 && <span className="muted-text">None selected</span>}
            {draft.tags.map((t) => (
              <span key={t} className="chip sel">
                {t}
                <button type="button" aria-label={`Remove tag ${t}`} onClick={() => toggleTag(t)}>×</button>
              </span>
            ))}
          </div>
          <div className="taglist avail">
            {lists.tags.filter((t) => !draft.tags.includes(t)).map((t) => (
              <button type="button" key={t} className="chip" onClick={() => toggleTag(t)}>{t}</button>
            ))}
            <input
              className="input newtag" placeholder="+ New tag, Enter" value={newTag}
              onChange={(e) => setNewTag(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addNewTag(); } }}
            />
          </div>
        </div>
        <div className="c12 cropbar">
          <span className="label" style={{ margin: 0 }}>Image</span>
          <span className="badge info">{cropLabel(p)}</span>
          <button className="btn small" disabled={busy} onClick={() => run(() => props.onCrop("draw"))}>
            {props.cropping ? "Drag on the page…" : "Draw crop"}
          </button>
          <button className="btn small" disabled={busy} onClick={() => run(() => props.onCrop("whole"))}>Whole page</button>
          <button className="btn small" disabled={busy} onClick={() => run(() => props.onCrop("none"))}>No image</button>
        </div>
      </div>
      {p.extraction_flags.length > 0 && (
        <p style={{ margin: "8px 0 0", fontSize: 12, color: "var(--warn)" }}>Extraction flags: {p.extraction_flags.join(", ")}</p>
      )}
      {error && <p role="alert" style={{ margin: "8px 0 0", color: "var(--danger)", whiteSpace: "pre-wrap" }}>{error}</p>}
      {saved && !dirty && <p style={{ margin: "8px 0 0", color: "var(--ok)", fontWeight: 600 }}>✓ Changes saved</p>}
      <div className="footer">
        <span className="spacer" />
        {dirty && (
          <button className="btn" disabled={busy} onClick={() => setDraft(toDraft(p, lists, props.defaultCategory))}>Undo changes</button>
        )}
        <button
          className={`btn${dirty ? " primary" : ""}`} disabled={!dirty || busy}
          onClick={async () => { if (await run(() => props.onSave(changed))) setSaved(true); }}
        >
          {busy ? "Saving…" : "Save changes"}
        </button>
      </div>
    </div>
  );
}
