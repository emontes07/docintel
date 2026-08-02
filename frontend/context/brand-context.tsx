"use client";

import * as React from "react";

import {
  BRANDS,
  BRAND_LIST,
  BRAND_STORAGE_KEY,
  DEFAULT_BRAND_ID,
  serializeBrandCss,
  type Brand,
  type BrandId,
} from "@/utils/brands";

interface BrandContextValue {
  brand: Brand;
  brandId: BrandId;
  brands: Brand[];
  setBrandId: (id: BrandId) => void;
}

const BrandContext = React.createContext<BrandContextValue | undefined>(
  undefined,
);

const STYLE_ELEMENT_ID = "__brand_theme__";

function readInitialBrandId(): BrandId {
  if (typeof window === "undefined") return DEFAULT_BRAND_ID;
  try {
    const stored = window.localStorage.getItem(BRAND_STORAGE_KEY);
    if (stored && stored in BRANDS) return stored as BrandId;
  } catch {
    /* localStorage may be blocked; fall through */
  }
  return DEFAULT_BRAND_ID;
}

export function BrandProvider({ children }: { children: React.ReactNode }) {
  // Start with default on the server + first client render to keep hydration
  // stable; hydrate the persisted brand right after mount.
  const [brandId, setBrandIdState] = React.useState<BrandId>(DEFAULT_BRAND_ID);

  React.useEffect(() => {
    setBrandIdState(readInitialBrandId());
  }, []);

  // Apply the active brand: set data attribute on <html> and inject the
  // scoped CSS override block (idempotent — replaces the same style tag).
  React.useEffect(() => {
    if (typeof document === "undefined") return;

    const html = document.documentElement;
    html.setAttribute("data-brand", brandId);

    // Build a CSS string containing overrides for EVERY brand, each scoped
    // by its own [data-brand] selector. That way switching is instant and
    // no other DOM mutation is needed.
    const css = BRAND_LIST.map(serializeBrandCss).filter(Boolean).join("\n\n");

    let styleEl = document.getElementById(
      STYLE_ELEMENT_ID,
    ) as HTMLStyleElement | null;
    if (!styleEl) {
      styleEl = document.createElement("style");
      styleEl.id = STYLE_ELEMENT_ID;
      document.head.appendChild(styleEl);
    }
    if (styleEl.textContent !== css) {
      styleEl.textContent = css;
    }
  }, [brandId]);

  const setBrandId = React.useCallback((id: BrandId) => {
    setBrandIdState(id);
    try {
      window.localStorage.setItem(BRAND_STORAGE_KEY, id);
    } catch {
      /* ignore */
    }
  }, []);

  const value = React.useMemo<BrandContextValue>(
    () => ({
      brand: BRANDS[brandId],
      brandId,
      brands: BRAND_LIST,
      setBrandId,
    }),
    [brandId, setBrandId],
  );

  return (
    <BrandContext.Provider value={value}>{children}</BrandContext.Provider>
  );
}

/**
 * Read the active brand. Safe to call from any client component below
 * <BrandProvider>. Falls back to the default brand outside the tree so
 * server rendering never crashes.
 */
export function useBrand(): BrandContextValue {
  const ctx = React.useContext(BrandContext);
  if (ctx) return ctx;
  return {
    brand: BRANDS[DEFAULT_BRAND_ID],
    brandId: DEFAULT_BRAND_ID,
    brands: BRAND_LIST,
    setBrandId: () => {
      /* no-op outside provider */
    },
  };
}
