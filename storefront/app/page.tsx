import Link from "next/link";
import SiteHeader, { Logo } from "@/components/SiteHeader";
import Slot from "@/components/Slot";
import { CATEGORIES, CLIENT_SLOTS, CONTACT, FAQ, MILESTONES, SERVICES, SITE_URL, TIERS, WAYS } from "@/lib/content";

const SIGN_IN = "/signin?next=/catalogue";

// Structured data for search engines and AI assistants. Only facts from the existing GIFTSYZ page.
const jsonLd = {
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Organization",
      name: "GIFTSYZ",
      url: SITE_URL,
      description: "Corporate gifting company in Bengaluru: curated corporate hampers, festive and occasion gifting, and sustainable gifts.",
      parentOrganization: { "@type": "Organization", name: "4SYZ", url: CONTACT.parentSite },
      areaServed: { "@type": "City", name: "Bengaluru" },
      email: CONTACT.email,
      telephone: CONTACT.whatsapp,
      slogan: "Gifts with Intention",
    },
    {
      "@type": "FAQPage",
      mainEntity: FAQ.map((f) => ({ "@type": "Question", name: f.q, acceptedAnswer: { "@type": "Answer", text: f.a } })),
    },
  ],
};

export default function Landing() {
  return (
    <>
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }} />
      <SiteHeader landing />
      <main>
        <section className="container hero" aria-labelledby="hero-title">
          <div>
            <h1 id="hero-title">Corporate gifts, chosen with intention.</h1>
            <p className="hero-sub">
              GIFTSYZ is a corporate gifting company in Bengaluru. Browse curated hampers, festive collections and
              sustainable gifts, shortlist what suits your team, and get a quote from people who answer.
            </p>
            <div className="hero-actions">
              <Link className="btn ink" href={SIGN_IN}>Browse the catalogue</Link>
              <a className="btn line" href="#ways">See how we gift</a>
            </div>
            <div className="facts">
              <div><b>Bengaluru</b>based</div>
              <div><b>Bulk pricing</b>on request</div>
              <div><b>Quotes</b>{CONTACT.responseTime}</div>
            </div>
          </div>
          <div className="hang" aria-hidden="true">
            <div className="hang-row">
              {[44, 96, 64, 132].map((len, i) => (
                <div className="hang-item" key={i} style={{ "--len": `${len}px`, "--i": i } as React.CSSProperties}>
                  <div className="thread" />
                  <div className="gift">
                    <Slot what="Hero product" shows="One hero product on a plain light background" format="Portrait 3:4" />
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="section alt" id="ways" aria-labelledby="ways-title">
          <div className="container">
            <div className="section-head">
              <h2 id="ways-title">Three ways to gift</h2>
              <p>Whatever the occasion, start with the approach that fits it.</p>
            </div>
            <div className="ways">
              {WAYS.map((w) => (
                <article className="way" key={w.name}>
                  <div className="art"><Slot {...w.slot} /></div>
                  <h3>{w.name}</h3>
                  <p>{w.text}</p>
                  <Link className="link" href={SIGN_IN}>Browse in the catalogue</Link>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="section" id="categories" aria-labelledby="cat-title">
          <div className="container">
            <div className="section-head">
              <h2 id="cat-title">Find gifts by category</h2>
              <p>Every category has subcategories to narrow things down once you are inside.</p>
            </div>
            <div className="cats">
              {CATEGORIES.map((c) => (
                <div className="cat-tile" key={c.name}>
                  <Link href={SIGN_IN}>
                    <div className="art"><Slot {...c.slot} /></div>
                    <h3>{c.name}</h3>
                  </Link>
                  <div className="tags">{c.subs.map((s) => <span className="tag" key={s}>{s}</span>)}</div>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="section alt" id="tiers" aria-labelledby="tiers-title">
          <div className="container">
            <div className="section-head">
              <h2 id="tiers-title">Start from your budget</h2>
              <p>Pick the figure per gift and see what is available at or below it.</p>
            </div>
            <div className="tiers">
              {TIERS.map((t) => (
                <Link className="tier" key={t.name} href={`/signin?next=${encodeURIComponent(`/catalogue?max=${t.upTo}`)}`}>
                  <div className="amount">₹{t.upTo.toLocaleString("en-IN")}</div>
                  <h3>{t.name}</h3>
                  <p>Hampers and gifts up to ₹{t.upTo.toLocaleString("en-IN")}</p>
                  <span className="go">See these gifts</span>
                </Link>
              ))}
            </div>
          </div>
        </section>

        <section className="section" id="clients" aria-labelledby="clients-title">
          <div className="container">
            <div className="section-head">
              <h2 id="clients-title">Teams we have gifted for</h2>
              <p>Placeholder: logos of clients we have worked with go here once confirmed.</p>
            </div>
            <div className="clients">
              {CLIENT_SLOTS.map((s, i) => <div className="art" key={i}><Slot {...s} /></div>)}
            </div>
          </div>
        </section>

        <section className="section ink" aria-labelledby="browse-title">
          <div className="container band">
            <div>
              <h2 id="browse-title">See every product, with prices.</h2>
              <p style={{ marginTop: 16, color: "#c8cdc9", maxWidth: "52ch" }}>
                Sign in to search and filter the catalogue, keep a wishlist with the quantities you need, and ask for a quote.
              </p>
            </div>
            <Link className="btn gold" href={SIGN_IN}>Sign in to browse</Link>
          </div>
        </section>

        <section className="section" aria-labelledby="timeline-title">
          <div className="container">
            <div className="section-head">
              <h2 id="timeline-title">How we have grown</h2>
              <p>Placeholder: real milestones go here once confirmed.</p>
            </div>
            <div className="timeline">
              {MILESTONES.map((m, i) => (
                <div className="milestone" key={i}><b>{m.year}</b><p>{m.text}</p></div>
              ))}
            </div>
          </div>
        </section>

        <section className="section alt" id="services" aria-labelledby="services-title">
          <div className="container">
            <div className="section-head">
              <h2 id="services-title">More from 4SYZ</h2>
              <p>Gifting is one part of what 4SYZ does for workplaces.</p>
            </div>
            <div className="services">
              {SERVICES.map((s) => (
                <div className="service" key={s.name}><h3>{s.name}</h3><p>{s.text}</p></div>
              ))}
            </div>
            <a className="btn line" href={CONTACT.parentSite} rel="noopener">Explore all services</a>
          </div>
        </section>

        <section className="section" id="faq" aria-labelledby="faq-title">
          <div className="container">
            <div className="section-head"><h2 id="faq-title">Questions we are asked</h2></div>
            <div className="faq">
              {FAQ.map((f) => (
                <details key={f.q}><summary>{f.q}</summary><p>{f.a}</p></details>
              ))}
            </div>
          </div>
        </section>

        <section className="section ink" aria-labelledby="final-title">
          <div className="container band">
            <div>
              <h2 id="final-title">Tell us what you are planning.</h2>
              <p style={{ marginTop: 16, color: "#c8cdc9" }}>
                Write to <a href={`mailto:${CONTACT.email}`} style={{ textDecoration: "underline" }}>{CONTACT.email}</a> or
                message us on <a href={CONTACT.whatsappLink} style={{ textDecoration: "underline" }}>WhatsApp</a>. We reply {CONTACT.responseTime}.
              </p>
            </div>
            <Link className="btn gold" href={SIGN_IN}>Browse the catalogue</Link>
          </div>
        </section>
      </main>

      <footer className="footer">
        <div className="container footer-grid">
          <div>
            <Logo />
            <p className="tagline"><span>शुभाशय</span> <em>Gifts with Intention</em></p>
            <p style={{ marginTop: 12 }}>
              GIFTSYZ is the corporate gifting arm of 4SYZ, a workplace services company based in Bengaluru.
              Shubhashaya is our luxury gifting line.
            </p>
          </div>
          <div>
            <h4>Explore</h4>
            <ul>
              <li><a href="#ways">Ways to gift</a></li>
              <li><a href="#tiers">Budgets</a></li>
              <li><a href="#categories">Categories</a></li>
              <li><Link href={SIGN_IN}>Sign in</Link></li>
            </ul>
          </div>
          <div>
            <h4>Contact</h4>
            <ul>
              <li><a href={`mailto:${CONTACT.email}`}>{CONTACT.email}</a></li>
              <li><a href={CONTACT.whatsappLink}>WhatsApp {CONTACT.whatsapp}</a></li>
              <li><a href={CONTACT.parentSite} rel="noopener">4syz.com</a></li>
            </ul>
          </div>
        </div>
        <div className="container"><small>© 4SYZ. Corporate gifting in Bengaluru.</small></div>
      </footer>
    </>
  );
}
