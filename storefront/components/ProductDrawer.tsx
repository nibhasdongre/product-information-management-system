"use client";

import { useEffect, useRef, useState } from "react";
import { useApp } from "@/lib/app";
import { call, money, type Card } from "@/lib/supabase";
import { CloseIcon, HeartIcon } from "./Icons";
import { ProductImage } from "./ProductCard";

export default function ProductDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const { wishlistIds, toggleWishlist } = useApp();
  const [loaded, setLoaded] = useState<{ id: number; card: Card | null; error: string } | null>(null);
  const [error, setError] = useState("");
  const close = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let cancelled = false;
    call<Card>("sf_get_product", { pid: id })
      .then((card) => !cancelled && setLoaded({ id, card, error: "" }))
      .catch((e) => !cancelled && setLoaded({ id, card: null, error: e instanceof Error ? e.message : String(e) }));
    return () => { cancelled = true; };
  }, [id]);

  useEffect(() => {
    close.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const ready = loaded?.id === id;
  const card = ready ? loaded.card : null;
  const listed = wishlistIds.has(id);

  async function toggle() {
    setError("");
    try { await toggleWishlist(id); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }

  const details: [string, string | null | undefined][] = card ? [
    ["Variant", card.variant],
    ["Unit", card.uom],
    ["Weight", card.weight != null ? `${card.weight} ${card.weight_unit ?? ""}`.trim() : null],
    ["Brand", card.brand_name],
  ] : [];

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={card?.product_name ?? "Product details"}>
        <button ref={close} className="btn line sm close" onClick={onClose} aria-label="Close"><CloseIcon size={16} /> Close</button>
        {!ready && <p className="empty">Opening…</p>}
        {ready && loaded.error && <p className="alert" role="alert" style={{ marginTop: 56 }}>{loaded.error}</p>}
        {card && (
          <>
            <div className="pimg big"><ProductImage path={card.image_path} name={card.product_name} size="detail" /></div>
            <h2>{card.product_name}</h2>
            <p className="crumbs">{[card.category_name, card.subcat_name].filter(Boolean).join(" / ") || card.brand_name}</p>
            <p className="price">
              {money(card.price_amount, card.price_currency)}
              {card.tax_note && <small>{card.tax_note}</small>}
            </p>
            {card.product_description && <p className="desc">{card.product_description}</p>}
            <dl>
              {details.filter(([, v]) => v).map(([k, v]) => <div key={k} style={{ display: "contents" }}><dt>{k}</dt><dd>{v}</dd></div>)}
              {card.tags.length > 0 && (
                <div style={{ display: "contents" }}>
                  <dt>Tags</dt>
                  <dd style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>{card.tags.map((t) => <span className="tag" key={t}>{t}</span>)}</dd>
                </div>
              )}
            </dl>
            <div className="actions">
              <button className={`btn ${listed ? "line" : "ink"}`} onClick={toggle} aria-pressed={listed}>
                <HeartIcon filled={listed} size={18} /> {listed ? "In your wishlist" : "Add to wishlist"}
              </button>
            </div>
            {error && <p className="alert" role="alert" style={{ marginTop: 12 }}>{error}</p>}
          </>
        )}
      </aside>
    </>
  );
}
