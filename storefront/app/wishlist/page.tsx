"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import Gate from "@/components/Gate";
import { ProductImage } from "@/components/ProductCard";
import SiteHeader from "@/components/SiteHeader";
import { useApp } from "@/lib/app";
import { call, money, type Card } from "@/lib/supabase";

export default function WishlistPage() {
  return (
    <>
      <SiteHeader />
      <Suspense fallback={<p className="empty">Opening…</p>}>
        <Gate><Wishlist /></Gate>
      </Suspense>
    </>
  );
}

function Wishlist() {
  const { setWishlistIds } = useApp();
  const [items, setItems] = useState<Card[] | null>(null);
  const [error, setError] = useState("");
  const [soon, setSoon] = useState("");

  useEffect(() => {
    call<{ items: Card[] }>("sf_wishlist")
      .then((r) => setItems(r.items))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  async function remove(id: number) {
    setError("");
    try {
      await call("sf_wishlist_remove", { pid: id });
      const next = (items ?? []).filter((i) => i.product_id !== id);
      setItems(next);
      setWishlistIds(new Set(next.map((i) => i.product_id)));
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }

  async function save(item: Card, qtyText: string, note: string) {
    const qty = qtyText.trim() === "" ? null : Number(qtyText);
    if (qty === (item.quantity ?? null) && note.trim() === (item.customization ?? "")) return;
    setError("");
    try {
      const updated = await call<Card>("sf_wishlist_update", { pid: item.product_id, qty, note });
      setItems((cur) => (cur ?? []).map((i) => (i.product_id === item.product_id ? updated : i)));
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }

  const withQty = items?.filter((i) => i.quantity).length ?? 0;

  return (
    <main className="container wl">
      <section>
        <h1>My wishlist</h1>
        <p className="lead">Add the quantity you expect and any customization for each product, then review before you ask for a quote or a deck.</p>
        {error && <p className="alert" role="alert" style={{ marginBottom: 16 }}>{error}</p>}
        {!items && !error && <p className="empty">Opening…</p>}
        {items && items.length === 0 && (
          <div className="empty" style={{ borderTop: "1px solid var(--line)" }}>
            <h2>Your wishlist is empty</h2>
            <p>Tap the heart on any product to shortlist it.</p>
            <Link className="btn ink" href="/catalogue" style={{ marginTop: 16 }}>Browse the catalogue</Link>
          </div>
        )}
        {items?.map((item) => (
          <article className="wl-row" key={item.product_id}>
            <div className="pimg"><ProductImage path={item.image_path} name={item.product_name} size="card" /></div>
            <div>
              <h2>{item.product_name}{item.variant ? `, ${item.variant}` : ""}</h2>
              <p className="sub">{[item.brand_name, item.subcat_name].filter(Boolean).join(", ")} &middot; {money(item.price_amount, item.price_currency)}{item.uom ? ` per ${item.uom}` : ""}</p>
              <div className="wl-fields">
                <label className="field">
                  Estimated quantity
                  <input className="input" type="number" min={1} max={1000000} inputMode="numeric" defaultValue={item.quantity ?? ""}
                    onBlur={(e) => void save(item, e.target.value, (e.currentTarget.closest(".wl-fields")?.querySelector("textarea") as HTMLTextAreaElement).value)} />
                </label>
                <label className="field">
                  Customization
                  <textarea className="input" rows={2} maxLength={1000} defaultValue={item.customization ?? ""}
                    placeholder="Logo, colour, packaging or message"
                    onBlur={(e) => void save(item, (e.currentTarget.closest(".wl-fields")?.querySelector("input") as HTMLInputElement).value, e.target.value)} />
                </label>
              </div>
              <div className="bar">
                <button className="link" onClick={() => void remove(item.product_id)}>Remove from wishlist</button>
                {!item.quantity && <span style={{ color: "var(--muted)" }}>Add a quantity so the quote is accurate.</span>}
              </div>
            </div>
          </article>
        ))}
      </section>

      <aside className="wl-summary" aria-label="Wishlist summary">
        <h2>{items ? `${items.length} product${items.length === 1 ? "" : "s"}` : "Wishlist"}</h2>
        {items && items.length > 0 && <p style={{ color: "var(--ink-soft)" }}>{withQty} of {items.length} have a quantity.</p>}
        <button className="btn ink" aria-disabled={!items?.length} onClick={() => items?.length && setSoon("Downloading a deck is not available yet. We will switch it on soon.")}>Get PPT</button>
        <button className="btn gold" aria-disabled={!items?.length} onClick={() => items?.length && setSoon("Asking for a quote from here is not available yet. For now, write to hello@4syz.com with your list.")}>Ask for quote</button>
        {soon && <p className="soon" role="status">{soon}</p>}
        <Link className="link" href="/catalogue">Keep browsing</Link>
      </aside>
    </main>
  );
}
