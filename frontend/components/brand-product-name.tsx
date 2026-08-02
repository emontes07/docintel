"use client";

import { useBrand } from "@/context/brand-context";

/**
 * Renders the active brand's product name.
 * Extracted as its own client component so parent server components
 * (e.g., the login page, which contains an inline "use server" action)
 * can still be rendered on the server.
 */
export function BrandProductName() {
  const { brand } = useBrand();
  return <>{brand.productName}</>;
}
