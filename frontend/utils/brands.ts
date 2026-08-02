/**
 * Brand skin registry.
 *
 * A "brand" is a bundle of:
 *   - identity (product name, tagline)
 *   - logo asset paths (light + dark variants)
 *   - CSS variable overrides for the shadcn design tokens
 *     (applied on top of the default light/dark palettes)
 *   - optional decorative flourishes (e.g., rainbow top stripe)
 *
 * The active brand is stored in localStorage and applied by injecting a
 * <style> element that scopes overrides via [data-brand="..."] on <html>.
 * That keeps light/dark switching untouched — brands layer on top.
 */

export type BrandId = "default" | "crayola";

export type ThemeVars = Record<string, string>;

export interface BrandTheme {
  /** CSS variable overrides applied on top of :root (light mode). */
  light: ThemeVars;
  /** CSS variable overrides applied on top of .dark (dark mode). */
  dark: ThemeVars;
}

export interface Brand {
  id: BrandId;
  /** Human-facing brand name shown in the picker. */
  name: string;
  /** Product name shown in sidebar header, login page, etc. */
  productName: string;
  /** Short tagline — reserved for future use (marketing surfaces). */
  tagline?: string;
  /** Logo image paths. Light/dark refers to the *page* theme, not the logo. */
  logo: {
    light: string;
    dark: string;
    /** Fallback icon (Lucide name) if image assets are missing. */
    fallbackIcon?: string;
  };
  /** Color-token overrides. Empty objects mean "keep shadcn defaults". */
  theme: BrandTheme;
  /** Swatches shown in the brand picker (hex — for a visual preview only). */
  swatches: string[];
  /** Optional decorative top stripe (e.g., Crayola's rainbow bar). */
  topStripe?: string;
}

/**
 * Default brand — the original Visionary Lab neutral look.
 * Uses only shadcn defaults; no overrides applied.
 */
const DEFAULT_BRAND: Brand = {
  id: "default",
  name: "Default",
  productName: "Visionary Lab",
  tagline: "AI-powered content generation",
  logo: {
    light: "/logo/logo-dark.png", // dark logo on light page bg
    dark: "/logo/logo-light.png", // light logo on dark page bg
  },
  theme: { light: {}, dark: {} },
  swatches: ["#111111", "#e5e5e5"],
};

/**
 * Crayola brand — playful, family-friendly palette. Anchored on Crayola Red
 * with an accent of warm yellow and a full rainbow chart palette. Rounded
 * corners are bumped up so cards/buttons feel softer and more toy-like.
 * Note: logo files under /logo/crayola/ are placeholder crayons illustrations
 * (not the Crayola trademark). Replace with official artwork if licensed.
 */
const CRAYOLA_BRAND: Brand = {
  id: "crayola",
  name: "Crayola",
  productName: "Crayola AI Studio",
  tagline: "What do you want to create today?",
  logo: {
    light: "/logo/crayola/logo-light.svg",
    dark: "/logo/crayola/logo-dark.svg",
  },
  swatches: ["#ED0A3F", "#FFD800", "#00B4E0", "#00A651"],
  topStripe:
    "linear-gradient(90deg, #ED0A3F 0%, #FF7F00 20%, #FFD800 40%, #00A651 60%, #00B4E0 80%, #6B3FA0 100%)",
  theme: {
    light: {
      // Softer, more toy-like corners across every shadcn component.
      "--radius": "0.9rem",

      // ---- Body & surfaces ----
      // Warm off-white body so red pops without vibrating.
      "--background": "oklch(0.995 0.008 85)",
      "--foreground": "oklch(0.18 0.02 275)",
      "--card": "oklch(1 0 0)",
      "--card-foreground": "oklch(0.18 0.02 275)",
      "--popover": "oklch(1 0 0)",
      "--popover-foreground": "oklch(0.18 0.02 275)",

      // ---- Primary: Crayola Red for CTAs and focus ring ----
      "--primary": "oklch(0.605 0.234 25)",
      "--primary-foreground": "oklch(0.985 0 0)",
      "--ring": "oklch(0.605 0.234 25)",

      // ---- Secondary: warm cream / soft butter — used by secondary buttons
      //      and pill outlines. Keeps the interface warm without shouting. ----
      "--secondary": "oklch(0.965 0.045 92)",
      "--secondary-foreground": "oklch(0.28 0.06 60)",

      // ---- Muted: warm sand for muted surfaces and skeletons ----
      "--muted": "oklch(0.96 0.02 80)",
      "--muted-foreground": "oklch(0.5 0.03 60)",

      // ---- Accent: soft sky — used on hover / subtle highlights ----
      "--accent": "oklch(0.94 0.05 220)",
      "--accent-foreground": "oklch(0.28 0.15 240)",

      // ---- Destructive: deeper crimson so it reads distinct from primary ----
      "--destructive": "oklch(0.55 0.22 15)",

      // ---- Borders / inputs: soft warm gray ----
      "--border": "oklch(0.9 0.015 60)",
      "--input": "oklch(0.9 0.015 60)",

      // ---- Chart palette: the full Crayola rainbow ----
      "--chart-1": "oklch(0.605 0.234 25)",   // red
      "--chart-2": "oklch(0.73 0.19 60)",     // orange
      "--chart-3": "oklch(0.83 0.16 95)",     // yellow
      "--chart-4": "oklch(0.66 0.18 145)",    // green
      "--chart-5": "oklch(0.63 0.17 240)",    // blue

      // ---- Sidebar: warm cream panel with red accents ----
      "--sidebar": "oklch(0.985 0.015 85)",
      "--sidebar-foreground": "oklch(0.22 0.02 275)",
      "--sidebar-primary": "oklch(0.605 0.234 25)",
      "--sidebar-primary-foreground": "oklch(0.985 0 0)",
      "--sidebar-accent": "oklch(0.94 0.045 90)",
      "--sidebar-accent-foreground": "oklch(0.28 0.15 25)",
      "--sidebar-border": "oklch(0.9 0.02 70)",
      "--sidebar-ring": "oklch(0.605 0.234 25)",
    },
    dark: {
      "--radius": "0.9rem",

      // Slightly warmer near-black so the palette doesn't feel cold.
      "--background": "oklch(0.16 0.015 30)",
      "--foreground": "oklch(0.98 0.005 80)",
      "--card": "oklch(0.22 0.02 30)",
      "--card-foreground": "oklch(0.98 0.005 80)",
      "--popover": "oklch(0.22 0.02 30)",
      "--popover-foreground": "oklch(0.98 0.005 80)",

      // Brighter red for dark bg legibility.
      "--primary": "oklch(0.68 0.23 25)",
      "--primary-foreground": "oklch(0.985 0 0)",
      "--ring": "oklch(0.68 0.23 25)",

      "--secondary": "oklch(0.3 0.06 60)",
      "--secondary-foreground": "oklch(0.98 0.005 80)",

      "--muted": "oklch(0.28 0.02 40)",
      "--muted-foreground": "oklch(0.72 0.02 60)",

      "--accent": "oklch(0.32 0.09 240)",
      "--accent-foreground": "oklch(0.98 0.005 80)",

      "--destructive": "oklch(0.7 0.2 15)",

      "--border": "oklch(1 0 0 / 12%)",
      "--input": "oklch(1 0 0 / 15%)",

      "--chart-1": "oklch(0.68 0.23 25)",
      "--chart-2": "oklch(0.78 0.18 60)",
      "--chart-3": "oklch(0.85 0.17 95)",
      "--chart-4": "oklch(0.72 0.18 145)",
      "--chart-5": "oklch(0.7 0.17 240)",

      "--sidebar": "oklch(0.2 0.02 30)",
      "--sidebar-foreground": "oklch(0.98 0.005 80)",
      "--sidebar-primary": "oklch(0.68 0.23 25)",
      "--sidebar-primary-foreground": "oklch(0.985 0 0)",
      "--sidebar-accent": "oklch(0.3 0.06 60)",
      "--sidebar-accent-foreground": "oklch(0.98 0.005 80)",
      "--sidebar-border": "oklch(1 0 0 / 10%)",
      "--sidebar-ring": "oklch(0.68 0.23 25)",
    },
  },
};

export const BRANDS: Record<BrandId, Brand> = {
  default: DEFAULT_BRAND,
  crayola: CRAYOLA_BRAND,
};

export const BRAND_LIST: Brand[] = [DEFAULT_BRAND, CRAYOLA_BRAND];

export const DEFAULT_BRAND_ID: BrandId = "default";

/** localStorage key used by BrandProvider to persist the active brand. */
export const BRAND_STORAGE_KEY = "visionary-lab:brand";

/**
 * Serialize a brand's theme overrides into a CSS string that can be
 * injected via a <style> element and scoped by [data-brand].
 */
export function serializeBrandCss(brand: Brand): string {
  const toBlock = (vars: ThemeVars) =>
    Object.entries(vars)
      .map(([k, v]) => `  ${k}: ${v};`)
      .join("\n");

  const lightBlock = toBlock(brand.theme.light);
  const darkBlock = toBlock(brand.theme.dark);

  const parts: string[] = [];
  if (lightBlock) {
    parts.push(`html[data-brand="${brand.id}"] {\n${lightBlock}\n}`);
  }
  if (darkBlock) {
    parts.push(`html[data-brand="${brand.id}"].dark {\n${darkBlock}\n}`);
  }
  return parts.join("\n\n");
}
