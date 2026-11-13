"use client";

import { useEffect } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useApp } from "@/lib/app";
import { CONTACT } from "@/lib/content";

/** Shows its children only to someone who is signed in and has an account for the catalogue. */
export default function Gate({ children }: { children: React.ReactNode }) {
  const { ready, session, me, meError, signOut } = useApp();
  const router = useRouter();
  const path = usePathname();
  const query = useSearchParams().toString();
  const here = query ? `${path}?${query}` : path;

  useEffect(() => {
    if (ready && !session) router.replace(`/signin?next=${encodeURIComponent(here)}`);
  }, [ready, session, router, here]);

  if (!ready || !session) return <p className="empty">Opening…</p>;

  if (meError)
    return (
      <div className="container empty">
        <h2>This account cannot open the catalogue yet</h2>
        <p style={{ margin: "8px auto 20px", maxWidth: "46ch" }}>
          You are signed in, but the account has not been set up for the catalogue. Write to{" "}
          <a className="link" href={`mailto:${CONTACT.email}`}>{CONTACT.email}</a> and we will sort it out.
        </p>
        <button className="btn line" onClick={() => void signOut()}>Sign out</button>
      </div>
    );

  if (!me) return <p className="empty">Opening…</p>;
  return <>{children}</>;
}
