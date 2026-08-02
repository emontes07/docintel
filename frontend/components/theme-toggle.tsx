"use client";

import * as React from "react";
import { Check, Monitor, Moon, Palette, Sun } from "lucide-react";
import { useTheme } from "next-themes";

import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useBrand } from "@/context/brand-context";
import type { Brand, BrandId } from "@/utils/brands";
import { cn } from "@/utils/utils";

/**
 * Appearance menu: combines light/dark/system selection with the brand
 * (skin) picker. Rendered from the same trigger position as the old
 * standalone theme toggle so the sidebar footer layout is unchanged.
 */
export function ThemeToggle() {
  const { setTheme, theme, resolvedTheme } = useTheme();
  const { brand, brands, setBrandId } = useBrand();
  const [mounted, setMounted] = React.useState(false);

  React.useEffect(() => {
    setMounted(true);
  }, []);

  const effectiveTheme = mounted ? resolvedTheme ?? theme : "light";
  const themeOptions: {
    id: "light" | "dark" | "system";
    label: string;
    Icon: React.ComponentType<{ className?: string }>;
  }[] = [
    { id: "light", label: "Light", Icon: Sun },
    { id: "dark", label: "Dark", Icon: Moon },
    { id: "system", label: "System", Icon: Monitor },
  ];

  return (
    <Popover>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <PopoverTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="Appearance">
                {effectiveTheme === "dark" ? (
                  <Moon className="h-5 w-5" />
                ) : (
                  <Sun className="h-5 w-5" />
                )}
                <span className="sr-only">Open appearance menu</span>
              </Button>
            </PopoverTrigger>
          </TooltipTrigger>
          <TooltipContent>
            <p>Appearance</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>

      <PopoverContent align="end" side="top" className="w-72 p-3">
        {/* Mode */}
        <div className="space-y-2">
          <p className="text-xs font-medium text-muted-foreground px-1">
            Mode
          </p>
          <div className="grid grid-cols-3 gap-1 rounded-md border p-1">
            {themeOptions.map(({ id, label, Icon }) => {
              const active = (theme ?? "system") === id;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => setTheme(id)}
                  className={cn(
                    "flex flex-col items-center justify-center gap-1 rounded-sm px-2 py-2 text-xs transition-colors",
                    active
                      ? "bg-accent text-accent-foreground"
                      : "hover:bg-accent/50 text-muted-foreground",
                  )}
                  aria-pressed={active}
                >
                  <Icon className="h-4 w-4" />
                  <span>{label}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Brand */}
        <div className="mt-4 space-y-2">
          <div className="flex items-center gap-1.5 px-1">
            <Palette className="h-3.5 w-3.5 text-muted-foreground" />
            <p className="text-xs font-medium text-muted-foreground">Brand</p>
          </div>
          <div className="flex flex-col gap-1">
            {brands.map((b) => (
              <BrandRow
                key={b.id}
                brand={b}
                active={brand.id === b.id}
                onSelect={() => setBrandId(b.id as BrandId)}
              />
            ))}
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function BrandRow({
  brand,
  active,
  onSelect,
}: {
  brand: Brand;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "flex w-full items-center gap-3 rounded-md border p-2 text-left transition-colors",
        active
          ? "border-primary/60 bg-accent"
          : "border-border hover:bg-accent/50",
      )}
      aria-pressed={active}
    >
      <div className="flex -space-x-1">
        {brand.swatches.slice(0, 4).map((color, i) => (
          <span
            key={i}
            className="inline-block h-4 w-4 rounded-full border border-background"
            style={{ backgroundColor: color }}
          />
        ))}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium leading-tight truncate">
          {brand.name}
        </p>
        <p className="text-[11px] text-muted-foreground truncate">
          {brand.productName}
        </p>
      </div>
      {active && <Check className="h-4 w-4 text-primary" />}
    </button>
  );
}
