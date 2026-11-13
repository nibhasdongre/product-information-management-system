// Everything the landing page says lives here, so real content can be dropped in without touching layout.
// Facts marked "site" come from the existing GIFTSYZ page on 4syz.com. Placeholders are marked as such.
// Do not add client names, brand partnerships or milestones until they are real.

export const CONTACT = {
  email: "hello@4syz.com",
  whatsapp: "+91 98862 07881",
  whatsappLink: "https://wa.me/919886207881",
  responseTime: "within 4 business hours",   // site
  parentSite: "https://4syz.com",
};

export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://4syz.com/giftsyz/";

/** Three ways of gifting. Names are the three gifting pillars on the existing site; copy is placeholder. */
export const WAYS = [
  {
    name: "Corporate gifting",
    text: "Hampers and gifts for teams, clients and partners. Placeholder: one or two sentences on how this works.",
    slot: { what: "Office or team gifting scene", shows: "Colleagues opening a hamper at a desk", format: "Portrait 4:5" },
  },
  {
    name: "Wedding and festive",
    text: "Gifts for festivals, weddings and celebrations. Placeholder: one or two sentences on how this works.",
    slot: { what: "Festive hamper close-up", shows: "A festive hamper with warm, celebratory styling", format: "Portrait 4:5" },
  },
  {
    name: "Sustainable gifting",
    text: "Gifts made with natural and recycled materials. Placeholder: one or two sentences on how this works.",
    slot: { what: "Eco gift flat-lay", shows: "Natural materials: bamboo, jute, recycled paper", format: "Portrait 4:5" },
  },
];

/** Budget tiers. Names and prices are from the existing site; confirm before launch. */
export const TIERS = [
  { name: "The Seedling", upTo: 500 },
  { name: "The Bloom", upTo: 1000 },
  { name: "The Bliss", upTo: 1500 },
  { name: "The Signature", upTo: 2000 },
];

/** Placeholder categories and subcategories. The real ones will come from the catalogue. */
export const CATEGORIES = Array.from({ length: 6 }, (_, i) => ({
  name: `Category ${i + 1}`,
  subs: ["Subcategory A", "Subcategory B", "Subcategory C"],
  slot: { what: "Category image", shows: "Three or four products from this category, styled together", format: "Landscape 4:3" },
}));

export const CLIENT_SLOTS = Array.from({ length: 6 }, () => ({
  what: "Client logo", shows: "The client's logo on a transparent background", format: "Wide, about 3:1",
}));

export const MILESTONES = Array.from({ length: 5 }, (_, i) => ({
  year: i === 0 ? "Year" : "Year",
  text: "Milestone placeholder",
}));

/** Other services. Names are from the existing site; descriptions are placeholders. */
export const SERVICES = [
  { name: "Nourish", text: "Placeholder: one line on this service." },
  { name: "Culture", text: "Placeholder: one line on this service." },
  { name: "Core", text: "Placeholder: one line on this service." },
];

/** Answers a search engine or an AI assistant can quote. Every line is a fact from the existing site. */
export const FAQ = [
  {
    q: "What does GIFTSYZ do?",
    a: "GIFTSYZ is the corporate gifting arm of 4SYZ, a workplace services company based in Bengaluru. It offers curated corporate hampers, festive and occasion gifting, and sustainable gifts.",
  },
  {
    q: "Who is corporate gifting for?",
    a: "HR teams, office managers and client-facing teams buying for employees, clients and partners, as well as people buying festive and wedding gifts and brands that want eco-conscious gifting.",
  },
  {
    q: "How do I get a quotation?",
    a: "Sign in to the catalogue, shortlist products in your wishlist with the quantities you expect, and ask for a quote. The team replies with a custom proposal within 4 business hours.",
  },
  {
    q: "Is bulk pricing available?",
    a: "Yes. Minimum order quantities apply and bulk pricing is available on request.",
  },
  {
    q: "What is Shubhashaya?",
    a: "Shubhashaya is the luxury gifting line from GIFTSYZ, built around the idea of gifts with intention.",
  },
];
