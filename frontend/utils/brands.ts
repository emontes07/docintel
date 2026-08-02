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
 * Crayola brand — playful, family-friendly palette anchored on Crayola Red.
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
  // Rainbow stripe rendered as a thin bar at the very top of the viewport.
  topStripe:
    "linear-gradient(90deg, #ED0A3F 0%, #FF7F00 20%, #FFD800 40%, #00A651 60%, #00B4E0 80%, #6B3FA0 100%)",
  theme: {
    light: {
      // Crayola Red as the primary CTA color.
      "--primary": "oklch(0.605 0.234 25)",
      "--primary-foreground": "oklch(0.985 0 0)",
      "--ring": "oklch(0.605 0.234 25)",
      // Sidebar accents pick up the brand hue for hovered/active items.
      "--sidebar-primary": "oklch(0.605 0.234 25)",
      "--sidebar-primary-foreground": "oklch(0.985 0 0)",
      // Warm off-white body so red pops without vibrating.
      "--background": "oklch(0.995 0.005 80)",
      "--card": "oklch(1 0 0)",
      "--popover": "oklch(1 0 0)",
      // Softer border tone that reads as warm rather than pure gray.
      "--border": "oklch(0.92 0.01 60)",
      "--input": "oklch(0.92 0.01 60)",
    },
    dark: {
      "--primary": "oklch(0.655 0.234 25)",
      "--primary-foreground": "oklch(0.985 0 0)",
      "--ring": "oklch(0.655 0.234 25)",
      "--sidebar-primary": "oklch(0.655 0.234 25)",
      "--sidebar-primary-foreground": "oklch(0.985 0 0)",
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
