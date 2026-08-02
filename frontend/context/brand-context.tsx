"use client";

import * as React from "react";

import {
  BRANDS,
  BRAND_LIST,
  BRAND_QUERY_PARAM,
  BRAND_STORAGE_KEY,
  DEFAULT_BRAND_ID,
  getConfiguredDefaultBrandId,
  isBrandId,
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

/**
 * Resolve the brand to show, in precedence order:
 *   1. `?brand=` query parameter  — shareable demo links win over everything
 *   2. localStorage              — this browser's explicit prior choice
 *   3. NEXT_PUBLIC_DEFAULT_BRAND — what this deployment ships with
 *   4. the stock default brand
 *
 * `fromUrl` is reported back so the caller can persist the choice and clean
 * the parameter out of the address bar.
 */
function resolveInitialBrand(): { id: BrandId; fromUrl: boolean } {
  if (typeof window === "undefined") {
    return { id: getConfiguredDefaultBrandId(), fromUrl: false };
  }

  try {
    const fromUrl = new URLSearchParams(window.location.search).get(
      BRAND_QUERY_PARAM,
    );
    if (isBrandId(fromUrl)) return { id: fromUrl, fromUrl: true };
  } catch {
    /* malformed URL; fall through */
  }

  try {
    const stored = window.localStorage.getItem(BRAND_STORAGE_KEY);
    if (isBrandId(stored)) return { id: stored, fromUrl: false };
  } catch {
    /* localStorage may be blocked; fall through */
  }

  return { id: getConfiguredDefaultBrandId(), fromUrl: false };
}

export function BrandProvider({ children }: { children: React.ReactNode }) {
  // Seed with the deploy-time brand. It's a build-time constant, so the
  // server and the first client render agree and hydration stays stable —
  // and the configured brand paints immediately instead of flashing the
  // stock one. Per-browser and per-link overrides are applied on mount.
  const [brandId, setBrandIdState] = React.useState<BrandId>(
    getConfiguredDefaultBrandId,
  );

  React.useEffect(() => {
    const { id, fromUrl } = resolveInitialBrand();
    setBrandIdState(id);

    if (!fromUrl) return;

    // A link-driven brand becomes this browser's choice, then the parameter
    // is stripped so it doesn't keep overriding the picker on later reloads.
    try {
      window.localStorage.setItem(BRAND_STORAGE_KEY, id);
    } catch {
      /* ignore */
    }
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete(BRAND_QUERY_PARAM);
      window.history.replaceState(null, "", url.toString());
    } catch {
      /* ignore */
    }
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
