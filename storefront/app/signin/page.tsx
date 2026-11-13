"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import SiteHeader from "@/components/SiteHeader";
import { useApp } from "@/lib/app";
import { CONTACT } from "@/lib/content";
import { supabase } from "@/lib/supabase";

export default function SignInPage() {
  return (
    <>
      <SiteHeader />
      <Suspense fallback={null}>
        <SignIn />
      </Suspense>
    </>
  );
}

function SignIn() {
  const router = useRouter();
  const next = useSearchParams().get("next");
  // Only follow paths inside this site.
  const target = next && next.startsWith("/") && !next.startsWith("//") ? next : "/catalogue";
  const { ready, session } = useApp();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // The fields stay read-only until clicked, which stops browsers filling in saved logins on page load.
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    if (ready && session) router.replace(target);
  }, [ready, session, router, target]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    setBusy(false);
    setPassword("");
    if (error) setError(error.message === "Invalid login credentials" ? "That email and password do not match. Check them and try again." : error.message);
  }

  return (
    <main className="auth">
      <div className="auth-card">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/lotus.png" alt="" />
        <h1>Sign in</h1>
        <p className="sub">Sign in to browse the catalogue, build a wishlist and ask for a quote.</p>
        <form onSubmit={submit}>
          <label className="field">
            <span className="sr-only">Email</span>
            <input className="input" type="email" name="giftsyz-email" autoComplete="off" placeholder="Enter email-id"
              readOnly={!armed} onFocus={() => setArmed(true)} value={email} onChange={(e) => setEmail(e.target.value)} required />
          </label>
          <label className="field">
            <span className="sr-only">Password</span>
            <input className="input" type="password" name="giftsyz-password" autoComplete="new-password" placeholder="Enter password"
              readOnly={!armed} onFocus={() => setArmed(true)} value={password} onChange={(e) => setPassword(e.target.value)} required />
          </label>
          <button className="btn ink" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
          {error && <p className="alert" role="alert">{error}</p>}
        </form>
        <p className="sub" style={{ margin: "20px 0 0" }}>
          No account yet? Write to <a className="link" href={`mailto:${CONTACT.email}`}>{CONTACT.email}</a>.
        </p>
      </div>
    </main>
  );
}
