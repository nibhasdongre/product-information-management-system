import { createClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL!;

// Browser-side client. Only the publishable key belongs here, never the secret key.
export const supabase = createClient(url, process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!);

/** Call a database function. A refusal becomes an Error whose message is safe to show. */
export async function call<T = unknown>(fn: string, args: Record<string, unknown> = {}): Promise<T> {
  const { data, error } = await supabase.rpc(fn, args);
  if (error) throw new Error(error.message);
  return data as T;
}

export type Card = {
  product_id: number;
  product_name: string;
  variant: string | null;
  price_amount: number;
  price_currency: string;
  brand_id: number;
  brand_name: string;
  category_id: number | null;
  category_name: string | null;
  sub_id: number | null;
  subcat_name: string | null;
  tags: string[];
  image_path: string | null;
  in_wishlist: boolean;
  // only on the detail view and the wishlist
  product_description?: string | null;
  tax_note?: string | null;
  uom?: string | null;
  weight?: number | null;
  weight_unit?: string | null;
  // only on wishlist items
  quantity?: number | null;
  customization?: string | null;
};

export type SearchResult = { total: number; page: number; page_size: number; items: Card[] };

export type Filters = {
  total: number;
  price_min: number | null;
  price_max: number | null;
  brands: { id: number; name: string; count: number }[];
  categories: {
    id: number;
    name: string;
    count: number;
    subcategories: { id: number; name: string; count: number }[];
  }[];
  tags: { name: string; count: number }[];
};

export type Me = {
  user_id: string;
  email: string;
  company: string | null;
  kind: "customer" | "staff";
  wishlist_count: number;
};

/** Published product images live in a public bucket: <image_path>/card.webp and /detail.webp. */
export function imageUrl(path: string, size: "card" | "detail" = "card") {
  return `${url}/storage/v1/object/public/product-images/${path}/${size}.webp`;
}

export function money(amount: number, currency = "INR") {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency,
    maximumFractionDigits: Number.isInteger(amount) ? 0 : 2,
  }).format(amount);
}
