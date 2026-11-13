import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/content";

// The landing page is public; the catalogue sits behind sign-in, so crawlers have nothing to index there.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [{ userAgent: "*", allow: "/", disallow: ["/catalogue", "/wishlist"] }],
    sitemap: `${SITE_URL.replace(/\/$/, "")}/sitemap.xml`,
  };
}
