"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { call, supabase, type Card, type Me } from "@/lib/supabase";

type AppState = {
  ready: boolean;
  session: Session | null;
  me: Me | null;
  /** Why the signed-in person cannot use the catalogue (for example, no account set up for it). */
  meError: string;
  wishlistIds: Set<number>;
  toggleWishlist: (productId: number) => Promise<void>;
  /** Wishlist changed somewhere else (for example removed on the wishlist page). */
  setWishlistIds: (ids: Set<number>) => void;
  signOut: () => Promise<void>;
};

const Ctx = createContext<AppState | null>(null);

export function useApp() {
  const value = useContext(Ctx);
  if (!value) throw new Error("useApp must be used inside AppProvider");
  return value;
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [meError, setMeError] = useState("");
  const [wishlistIds, setWishlistIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setReady(true);
    });
    const { data } = supabase.auth.onAuthStateChange((_event, s) => setSession(s));
    return () => data.subscription.unsubscribe();
  }, []);

  const load = useCallback(async () => {
    try {
      const who = await call<Me>("sf_me");
      const list = await call<{ items: Card[] }>("sf_wishlist");
      setMe(who);
      setMeError("");
      setWishlistIds(new Set(list.items.map((i) => i.product_id)));
    } catch (e) {
      setMe(null);
      setMeError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    // Load who this is and what they have shortlisted; the database refuses anyone without an account.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (session) void load();
    else {
      setMe(null);
      setMeError("");
      setWishlistIds(new Set());
    }
  }, [session, load]);

  const toggleWishlist = useCallback(async (productId: number) => {
    const adding = !wishlistIds.has(productId);
    const next = new Set(wishlistIds);
    if (adding) next.add(productId);
    else next.delete(productId);
    setWishlistIds(next);                         // show the change at once
    try {
      await call(adding ? "sf_wishlist_add" : "sf_wishlist_remove", { pid: productId });
    } catch (e) {
      setWishlistIds(wishlistIds);                // put it back if the database said no
      throw e;
    }
  }, [wishlistIds]);

  const value = useMemo<AppState>(() => ({
    ready, session, me, meError, wishlistIds, toggleWishlist, setWishlistIds,
    signOut: async () => { await supabase.auth.signOut(); },
  }), [ready, session, me, meError, wishlistIds, toggleWishlist]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
