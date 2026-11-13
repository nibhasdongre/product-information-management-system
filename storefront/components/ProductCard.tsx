"use client";

import { useState } from "react";
import { imageUrl, money, type Card } from "@/lib/supabase";
import { HeartIcon } from "./Icons";

export function ProductImage({ path, name, size }: { path: string | null; name: string; size: "card" | "detail" }) {
  const [broken, setBroken] = useState(false);
  if (!path || broken) return <div className="noimg">Image coming soon</div>;
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={imageUrl(path, size)} alt={name} loading="lazy" onError={() => setBroken(true)} />;
}

export function HeartButton({ on, onToggle, label }: { on: boolean; onToggle: () => void; label: string }) {
  return (
    <button
      className="heart" aria-pressed={on} onClick={(e) => { e.stopPropagation(); onToggle(); }}
      aria-label={on ? `Remove ${label} from wishlist` : `Add ${label} to wishlist`}
      title={on ? "In your wishlist" : "Add to wishlist"}
    >
      <HeartIcon filled={on} />
    </button>
  );
}

export default function ProductCard({ card, listed, onOpen, onToggle }: {
  card: Card; listed: boolean; onOpen: () => void; onToggle: () => void;
}) {
  return (
    <article className="pcard">
      <HeartButton on={listed} onToggle={onToggle} label={card.product_name} />
      <button className="open" onClick={onOpen} aria-label={`View ${card.product_name}`}>
        <div className="pimg"><ProductImage path={card.image_path} name={card.product_name} size="card" /></div>
        <div className="meta">
          {card.subcat_name && <span className="sub">{card.subcat_name}</span>}
          <span className="name">{card.product_name}{card.variant ? `, ${card.variant}` : ""}</span>
          <span className="price">{money(card.price_amount, card.price_currency)}</span>
          {card.tags.length > 0 && (
            <span className="tags">{card.tags.slice(0, 3).map((t) => <span className="tag" key={t}>{t}</span>)}</span>
          )}
        </div>
      </button>
    </article>
  );
}
