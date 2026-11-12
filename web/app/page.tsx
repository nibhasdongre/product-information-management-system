"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import type { Session } from "@supabase/supabase-js";
import { supabase, type Catalog } from "@/lib/supabase";

type Me = { email: string; role: "reviewer" | "admin" };

const STATE: Record<string, { label: string; tone: string }> = {
  not_ready: { label: "Not ready", tone: "" },
  level_1_open: { label: "Level 1 open", tone: "info" },
  level_1_in_progress: { label: "Level 1 in progress", tone: "warn" },
  level_2_open: { label: "Level 2 open", tone: "info" },
  level_2_in_progress: { label: "Level 2 in progress", tone: "warn" },
  done: { label: "Done", tone: "ok" },
};

export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [me, setMe] = useState<Me | null>(null);
  const [catalogs, setCatalogs] = useState<Catalog[]>([]);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setReady(true);
    });
    const { data } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => data.subscription.unsubscribe();
  }, []);

  const load = useCallback(async () => {
    setError("");
    const who = await supabase.rpc("rv_me");
    if (who.error) {
      setMe(null);
      setError(who.error.message);
      return;
    }
    setMe(who.data as Me);
    const list = await supabase.rpc("rv_list_catalogs");
    if (list.error) setError(list.error.message);
    else setCatalogs(list.data as Catalog[]);
  }, []);

  useEffect(() => {
    // Load the signed-in user's catalogues; the database refuses anyone without a role.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (session) load();
    else {
      setMe(null);
      setCatalogs([]);
    }
  }, [session, load]);

  async function signIn(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) setError(error.message);
    setPassword("");
  }

  if (!ready) return null;

  if (!session)
    return (
      <main className="auth-wrap">
        <div className="auth-card">
          <h1>Catalogue review</h1>
          <p className="sub">Sign in with the account you were invited with.</p>
          <form onSubmit={signIn}>
            <div>
              <label className="label" htmlFor="email">Email</label>
              <input id="email" className="input" type="email" placeholder="Enter email-id" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </div>
            <div>
              <label className="label" htmlFor="password">Password</label>
              <input id="password" className="input" type="password" placeholder="Enter password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </div>
            <button className="btn primary" type="submit">Sign in</button>
            {error && <p role="alert" style={{ margin: 0, color: "var(--danger)" }}>{error}</p>}
          </form>
        </div>
      </main>
    );

  return (
    <>
      <header className="topbar">
        <h1>Catalogue review</h1>
        <span className="spacer" />
        <span className="muted">{me ? me.email : session.user.email}</span>
        {me && <span className="badge">{me.role}</span>}
        <button className="btn small" onClick={() => supabase.auth.signOut()}>Sign out</button>
      </header>
      <main className="page">
        {error && <p className="alert" role="alert" style={{ margin: "0 0 12px" }}>{error}</p>}
        <div className="card">
          <table className="table">
            <thead>
              <tr><th>Catalogue</th><th>Brand</th><th>Pages</th><th>Status</th><th>Level 1</th><th>Level 2</th><th /></tr>
            </thead>
            <tbody>
              {catalogs.map((c) => {
                const st = STATE[c.state] ?? { label: c.state.replaceAll("_", " "), tone: "" };
                return (
                  <tr key={c.catalog_id}>
                    <td><b>{c.catalog_name}</b></td>
                    <td>{c.brand_name}</td>
                    <td>{c.page_count}</td>
                    <td><span className={`badge ${st.tone}`}>{st.label}</span></td>
                    <td>{c.level_1_by ?? "–"}</td>
                    <td>{c.level_2_by ?? "–"}</td>
                    <td style={{ textAlign: "right" }}>
                      {c.can_open ? (
                        <Link className="btn primary small" href={`/review/${c.catalog_id}`}>
                          {c.mine ? "Resume" : "Start"} level {c.level}
                        </Link>
                      ) : (
                        <span className="muted">{c.blocked_reason}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
              {catalogs.length === 0 && !error && (
                <tr><td colSpan={7} className="muted" style={{ textAlign: "center", padding: 24 }}>No catalogues yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </main>
    </>
  );
}
