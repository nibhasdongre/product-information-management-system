import type { Metadata } from "next";
import { Cormorant_Garamond, DM_Sans, Noto_Serif_Devanagari } from "next/font/google";
import { AppProvider } from "@/lib/app";
import { SITE_URL } from "@/lib/content";
import "./globals.css";

const display = Cormorant_Garamond({
  variable: "--font-display",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  style: ["normal", "italic"],
});

const body = DM_Sans({
  variable: "--font-body",
  subsets: ["latin"],
});

const devanagari = Noto_Serif_Devanagari({
  variable: "--font-devanagari",
  subsets: ["devanagari"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: "GIFTSYZ | Corporate gifting in Bengaluru",
    template: "%s | GIFTSYZ",
  },
  description:
    "GIFTSYZ is a Bengaluru corporate gifting company from 4SYZ: curated corporate hampers, festive and occasion gifts, and sustainable gifting for teams and clients. Sign in to browse the catalogue and request a quote.",
  openGraph: {
    title: "GIFTSYZ | Corporate gifting in Bengaluru",
    description: "Curated corporate hampers, festive collections and sustainable gifts, with quotes within 4 business hours.",
    type: "website",
    locale: "en_IN",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${body.variable} ${devanagari.variable}`}>
      <body>
        <AppProvider>{children}</AppProvider>
      </body>
    </html>
  );
}
