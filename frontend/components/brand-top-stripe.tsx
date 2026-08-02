"use client";

import { useBrand } from "@/context/brand-context";

/**
 * Optional decorative top stripe for the active brand.
 * Renders a thin horizontal bar at the very top of the viewport when the
 * brand config supplies a `topStripe` CSS background value. No-ops for
 * brands that don't want it.
 */
export function BrandTopStripe() {
  const { brand } = useBrand();
  if (!brand.topStripe) return null;
  return (
    <div
      aria-hidden="true"
      className="fixed inset-x-0 top-0 z-50 h-1"
      style={{ background: brand.topStripe }}
    />
  );
}
