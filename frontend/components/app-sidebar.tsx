"use client"

import { Settings } from "lucide-react"
import Link from 'next/link';
import Image from 'next/image';
import { useTheme } from "next-themes";
import { ThemeToggle } from "@/components/theme-toggle";import { useBrand } from "@/context/brand-context";import { useEffect, useState, type CSSProperties } from "react";
import { usePathname } from "next/navigation";

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarHeader,
} from "@/components/ui/sidebar"

// Manage section items
const manageItems = [
  {
    title: "Settings",
    url: "/settings",
    icon: Settings,
    description: "Configure application settings"
  }
]

export function AppSidebar() {
  const { theme } = useTheme();
  const { brand } = useBrand();
  const [mounted, setMounted] = useState(false);
  const pathname = usePathname();
  
  // Only render logo after mounted on client to avoid hydration mismatch
  useEffect(() => {
    setMounted(true);
  }, []);

  // Determine logo based on active brand + page theme.
  // Each brand exposes light/dark variants where the name refers to which
  // page background the asset is designed to sit on top of.
  const logoSrc = mounted && theme === "dark"
    ? brand.logo.dark
    : brand.logo.light;

  // Per-item accent color from the active brand's nav palette (if any).
  // Returns undefined for brands that don't define one, so those keep the
  // stock neutral shadcn treatment.
  const navColor = (index: number): string | undefined => {
    const palette = brand.navPalette;
    if (!palette || palette.length === 0) return undefined;
    return palette[index % palette.length];
  };

  // Active nav items render as a soft pill in their own color. Falls back to
  // the sidebar accent token when the brand has no nav palette.
  const activePillStyle = (
    active: boolean,
    color?: string,
  ): CSSProperties | undefined => {
    if (!active || !color) return undefined;
    return {
      backgroundColor: `color-mix(in srgb, ${color} 14%, transparent)`,
      color,
    };
  };

  const navButtonClass = (color?: string) =>
    [
      "data-[active=true]:font-medium",
      "group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-2",
      // Only apply token-based active colors when the brand has no palette;
      // otherwise the inline pill style supplies them.
      color
        ? ""
        : "data-[active=true]:bg-sidebar-accent data-[active=true]:text-sidebar-accent-foreground",
    ]
      .filter(Boolean)
      .join(" ");

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="p-4 group-data-[collapsible=icon]:px-2 group-data-[collapsible=icon]:justify-center">
        {mounted ? (
          <>
            <div className="flex items-center group-data-[collapsible=icon]:hidden">
              <Image
                src={logoSrc}
                alt={brand.productName}
                width={30}
                height={30}
                className="mr-2"
                onError={(e) => {
                  // Fallback to SVG if PNG fails to load
                  const imgElement = e.currentTarget;
                  if (logoSrc.endsWith('.png')) {
                    imgElement.src = logoSrc.replace('.png', '.svg');
                  }
                }}
              />
              <h2 className="font-semibold text-lg">{brand.productName}</h2>
            </div>
            <div className="hidden group-data-[collapsible=icon]:flex items-center justify-center">
              <Image
                src={logoSrc}
                alt={brand.productName}
                width={24}
                height={24}
                onError={(e) => {
                  // Fallback to SVG if PNG fails to load
                  const imgElement = e.currentTarget;
                  if (logoSrc.endsWith('.png')) {
                    imgElement.src = logoSrc.replace('.png', '.svg');
                  }
                }}
              />
            </div>
          </>
        ) : (
          // Placeholder during SSR
          <div className="h-8 group-data-[collapsible=icon]:h-6"></div>
        )}
      </SidebarHeader>
      <SidebarContent>
        {/* Manage Section */}
        <SidebarGroup>
          <SidebarGroupLabel className="group-data-[collapsible=icon]:hidden">Manage</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {manageItems.map((item, i) => {
                const active = pathname === item.url;
                const color = navColor(i);
                return (
                  <SidebarMenuItem key={item.title}>
                    <Link href={item.url} passHref legacyBehavior>
                      <SidebarMenuButton
                        asChild
                        data-active={active}
                        className={navButtonClass(color)}
                        style={activePillStyle(active, color)}
                      >
                        <a title={item.description}>
                          <item.icon
                            className="h-4 w-4 group-data-[collapsible=icon]:h-5 group-data-[collapsible=icon]:w-5"
                            style={color ? { color } : undefined}
                          />
                          <span className="group-data-[collapsible=icon]:hidden">{item.title}</span>
                        </a>
                      </SidebarMenuButton>
                    </Link>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      
      {/* Add a footer with theme toggle */}
      <SidebarFooter className="p-4 group-data-[collapsible=icon]:p-2 border-t">
        <div className="flex items-center justify-between group-data-[collapsible=icon]:justify-center">
          <span className="text-sm text-muted-foreground group-data-[collapsible=icon]:hidden">Theme</span>
          <ThemeToggle />
        </div>
      </SidebarFooter>
    </Sidebar>
  )
} 