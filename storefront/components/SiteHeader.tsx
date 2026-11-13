"use client";

import Link from "next/link";
import { useApp } from "@/lib/app";
import { HeartIcon } from "./Icons";

export function Logo() {
  return (
    <Link href="/" className="brand" aria-label="GIFTSYZ home">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/lotus.png" alt="" width={39} height={30} />
      <span>GIFTSYZ</span>
    </Link>
  );
}

const LANDING_LINKS = [
  { href: "#ways", label: "Ways to gift" },
  { href: "#tiers", label: "Budgets" },
  { href: "#categories", label: "Categories" },
  { href: "#clients", label: "Clients" },
  { href: "#services", label: "Services" },
];

export default function SiteHeader({ landing = false }: { landing?: boolean }) {
  const { ready, session, me, wishlistIds, signOut } = useApp();
  const signedIn = ready && !!session;

  return (
    <header className="site-header">
      <div className="pill">
        <Logo />
        {landing && (
          <nav className="nav" aria-label="Sections">
            {LANDING_LINKS.map((l) => <a key={l.href} href={l.href}>{l.label}</a>)}
          </nav>
        )}
        <div className="pill-actions">
          {signedIn ? (
            <>
              {!landing && <Link href="/catalogue" className="heart-link">Catalogue</Link>}
              <Link href="/wishlist" className="heart-link" aria-label={`Wishlist, ${wishlistIds.size} products`}>
                <HeartIcon size={18} /> Wishlist
                {wishlistIds.size > 0 && <span className="count-badge">{wishlistIds.size}</span>}
              </Link>
              {landing && <Link href="/catalogue" className="btn ink sm">Browse the catalogue</Link>}
              <button className="btn line sm" onClick={() => void signOut()} title={me?.email}>Sign out</button>
            </>
          ) : (
            <>
              <Link href="/signin?next=/catalogue" className="heart-link">Sign in</Link>
              <Link href="/signin?next=/catalogue" className="btn ink sm">Browse the catalogue</Link>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
