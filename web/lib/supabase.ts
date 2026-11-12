import { createClient } from "@supabase/supabase-js";

// Browser-side client. Only the publishable key belongs here, never the secret key.
export const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
);

export type Catalog = {
  catalog_id: number;
  catalog_name: string;
  brand_name: string;
  page_count: number;
  ready: boolean;
  state: string;
  level_1_by: string | null;
  level_2_by: string | null;
  can_open: boolean;
  mine: boolean;
  level: number | null;
  blocked_reason: string | null;
};

export type Crop = { x: number; y: number; w: number; h: number } | { none: true } | null;

export type Product = {
  product_id: number | null;
  edit_id: number | null;
  product_name: string | null;
  product_description: string | null;
  variant: string | null;
  price_amount: number | null;
  price_currency: string;
  tax_note: string | null;
  uom: string | null;
  weight: number | null;
  weight_unit: string | null;
  sub_id: number | null;
  tags: string[];
  crop: Crop;
  confidence: number | null;
  extraction_flags: string[];
  removed: boolean;
  edited: boolean;
};

export type Lists = {
  brands: { id: number; name: string }[];
  categories: { id: number; name: string }[];
  subcategories: { id: number; name: string; category_id: number }[];
  tags: string[];
};

export type Progress = {
  catalog_id: number;
  catalog_name: string;
  brand_name: string;
  storage_path: string;
  level: number;
  page_count: number;
  seen_pages: number[];
  pages_seen: number;
  resume_page: number;
  can_finish: boolean;
  edit_count: number;
  products_per_page: number[];
  product_count: number;
};

export type PageData = { page: number; failed: boolean; page_type: string; products: Product[] };
