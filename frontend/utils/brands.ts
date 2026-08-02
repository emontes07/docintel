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
  /**
   * Optional per-item accent colors for sidebar navigation. When present,
   * each nav item cycles through this palette: the icon is tinted with its
   * color at rest, and the active item renders as a soft pill of the same
   * hue. This is what makes a brand feel multi-color rather than
   * single-hue-tinted. Omit to keep the neutral shadcn treatment.
   */
  navPalette?: string[];
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
  swatches: ["#ED0A3F", "#FF7F00", "#00A651", "#0093D1"],
  // Every nav item gets its own crayon. Colors are chosen to stay legible
  // as text on a white surface (yellow is darkened to amber for contrast).
  navPalette: [
    "#ED0A3F", // scarlet
    "#0093D1", // cerulean
    "#00A651", // green
    "#8B3FA8", // violet
    "#F2670B", // orange
    "#0F9B9B", // teal
    "#C2185B", // magenta
    "#B8860B", // goldenrod
  ],
  topStripe:
    "linear-gradient(90deg, #ED0A3F 0%, #FF7F00 20%, #FFD800 40%, #00A651 60%, #00B4E0 80%, #6B3FA0 100%)",
  theme: {
    light: {
      // Soft, toy-like corners.
      "--radius": "1rem",

      // ---- Surfaces: clean white. The color comes from content and nav
      //      accents, not from a background wash. ----
      "--background": "oklch(1 0 0)",
      "--foreground": "oklch(0.2 0.02 275)",
      "--card": "oklch(1 0 0)",
      "--card-foreground": "oklch(0.2 0.02 275)",
      "--popover": "oklch(1 0 0)",
      "--popover-foreground": "oklch(0.2 0.02 275)",

      // ---- Primary: Crayola Red for CTAs and focus ring ----
      "--primary": "oklch(0.605 0.234 25)",
      "--primary-foreground": "oklch(0.985 0 0)",
      "--ring": "oklch(0.605 0.234 25)",

      // ---- Neutral supporting tones so the rainbow accents stay the star ----
      "--secondary": "oklch(0.965 0.004 250)",
      "--secondary-foreground": "oklch(0.25 0.02 275)",
      "--muted": "oklch(0.965 0.004 250)",
      "--muted-foreground": "oklch(0.53 0.015 265)",
      "--accent": "oklch(0.955 0.006 250)",
      "--accent-foreground": "oklch(0.22 0.02 275)",

      "--destructive": "oklch(0.577 0.245 27.325)",
      "--border": "oklch(0.925 0.004 250)",
      "--input": "oklch(0.925 0.004 250)",

      // ---- Chart palette: the full Crayola rainbow ----
      "--chart-1": "oklch(0.605 0.234 25)",   // red
      "--chart-2": "oklch(0.73 0.19 60)",     // orange
      "--chart-3": "oklch(0.83 0.16 95)",     // yellow
      "--chart-4": "oklch(0.66 0.18 145)",    // green
      "--chart-5": "oklch(0.63 0.17 240)",    // blue

      // ---- Sidebar: near-white panel; per-item colors supply the character ----
      "--sidebar": "oklch(0.99 0.002 250)",
      "--sidebar-foreground": "oklch(0.24 0.02 275)",
      "--sidebar-primary": "oklch(0.605 0.234 25)",
      "--sidebar-primary-foreground": "oklch(0.985 0 0)",
      "--sidebar-accent": "oklch(0.955 0.006 250)",
      "--sidebar-accent-foreground": "oklch(0.22 0.02 275)",
      "--sidebar-border": "oklch(0.925 0.004 250)",
      "--sidebar-ring": "oklch(0.605 0.234 25)",
    },
    dark: {
      "--radius": "1rem",

      "--background": "oklch(0.15 0.01 275)",
      "--foreground": "oklch(0.98 0.002 250)",
      "--card": "oklch(0.21 0.012 275)",
      "--card-foreground": "oklch(0.98 0.002 250)",
      "--popover": "oklch(0.21 0.012 275)",
      "--popover-foreground": "oklch(0.98 0.002 250)",

      "--primary": "oklch(0.68 0.23 25)",
      "--primary-foreground": "oklch(0.985 0 0)",
      "--ring": "oklch(0.68 0.23 25)",

      "--secondary": "oklch(0.27 0.012 275)",
      "--secondary-foreground": "oklch(0.98 0.002 250)",
      "--muted": "oklch(0.27 0.012 275)",
      "--muted-foreground": "oklch(0.72 0.015 265)",
      "--accent": "oklch(0.29 0.014 275)",
      "--accent-foreground": "oklch(0.98 0.002 250)",

      "--destructive": "oklch(0.704 0.191 22.216)",
      "--border": "oklch(1 0 0 / 12%)",
      "--input": "oklch(1 0 0 / 15%)",

      "--chart-1": "oklch(0.68 0.23 25)",
      "--chart-2": "oklch(0.78 0.18 60)",
      "--chart-3": "oklch(0.85 0.17 95)",
      "--chart-4": "oklch(0.72 0.18 145)",
      "--chart-5": "oklch(0.7 0.17 240)",

      "--sidebar": "oklch(0.19 0.012 275)",
      "--sidebar-foreground": "oklch(0.98 0.002 250)",
      "--sidebar-primary": "oklch(0.68 0.23 25)",
      "--sidebar-primary-foreground": "oklch(0.985 0 0)",
      "--sidebar-accent": "oklch(0.29 0.014 275)",
      "--sidebar-accent-foreground": "oklch(0.98 0.002 250)",
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
