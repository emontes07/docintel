"use client"

import { ImageIcon, FolderIcon, ImagePlus, Settings, ChevronDown, Pencil, Loader2, Search } from "lucide-react"
import Link from 'next/link';
import Image from 'next/image';
import { useTheme } from "next-themes";
import { ThemeToggle } from "@/components/theme-toggle";import { useBrand } from "@/context/brand-context";import { useEffect, useState, type CSSProperties } from "react";
import { fetchFolders, MediaType } from "@/services/api";
import { useRouter, usePathname, useSearchParams } from "next/navigation";
import { useFolderContext } from "@/context/folder-context";
import { motion } from "framer-motion";

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
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Skeleton } from "@/components/ui/skeleton";

// Create section items
const createItems = [
  {
    title: "New Image",
    url: "/new-image",
    icon: ImagePlus,
    description: "Generate new images with AI"
  },
  {
    title: "Edit Image",
    url: "/edit-image",
    icon: Pencil,
    description: "Edit and enhance existing images"
  },
  {
    title: "Analyze",
    url: "/analyze",
    icon: Search,
    description: "Custom image analysis with AI"
  }
]

// Manage section items
const manageItems = [
  {
    title: "Settings",
    url: "/settings",
    icon: Settings,
    description: "Configure application settings"
  }
]

// Animation variants for folder items
const folderItemVariants = {
  hidden: { opacity: 0, y: 10 },
  visible: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: {
      delay: i * 0.05,
      duration: 0.3,
    }
  })
};

export function AppSidebar() {
  const { theme } = useTheme();
  const { brand } = useBrand();
  const [mounted, setMounted] = useState(false);
  const [imageFolders, setImageFolders] = useState<string[]>([]);
  const [isImageFoldersOpen, setIsImageFoldersOpen] = useState(true);
  const [isImageFoldersLoading, setIsImageFoldersLoading] = useState(true);
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const currentFolderParam = searchParams.get('folder');
  const { folderRefreshTrigger } = useFolderContext();
  
  // Only render logo after mounted on client to avoid hydration mismatch
  useEffect(() => {
    setMounted(true);
  }, []);

  // Fetch folders on component mount and when refresh is triggered
  useEffect(() => {
    const loadImageFolders = async () => {
      setIsImageFoldersLoading(true);
      try {
        const response = await fetchFolders(MediaType.IMAGE);
        setImageFolders(response.folders);
      } catch (error) {
        console.error("Error fetching image folders:", error);
      } finally {
        setIsImageFoldersLoading(false);
      }
    };

    loadImageFolders();
  }, [folderRefreshTrigger]); // Re-run when folders are created/updated

  // Determine logo based on active brand + page theme.
  // Each brand exposes light/dark variants where the name refers to which
  // page background the asset is designed to sit on top of.
  const logoSrc = mounted && theme === "dark"
    ? brand.logo.dark
    : brand.logo.light;
    
  // Navigate to images page with folder filter
  const handleImageFolderClick = (folderPath: string) => {
    router.push(`/new-image?folder=${encodeURIComponent(folderPath)}`);
  };

  // Check if an image folder link is active
  const isImageFolderActive = (folderPath: string | null) => {
    if (!folderPath) {
      // "All Images" is active when no folder parameter is present
      return pathname === '/new-image' && !currentFolderParam;
    }
    
    // Otherwise check if the folder parameter matches the current folder
    return pathname === '/new-image' && currentFolderParam === folderPath;
  };

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

  // Render folder skeletons during loading
  const renderFolderSkeletons = (count: number = 3) => {    return Array.from({ length: count }).map((_, index) => (
      <SidebarMenuItem key={`folder-skeleton-${index}`}>
        <div className="px-3 py-2 flex items-center w-full">
          <Skeleton className="h-4 w-4 mr-2 rounded-sm" />
          <Skeleton className="h-4 w-24 rounded-sm" />
        </div>
      </SidebarMenuItem>
    ));
  };

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
        {/* Create Section */}
        <SidebarGroup>
          <SidebarGroupLabel className="group-data-[collapsible=icon]:hidden">Create</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {createItems.map((item, i) => {
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

        {/* Image Folders Section */}
        <div className="group-data-[collapsible=icon]:hidden">
          <Collapsible
            open={isImageFoldersOpen}
            onOpenChange={setIsImageFoldersOpen}
            className="w-full"
          >
            <SidebarGroup>
              <SidebarGroupLabel asChild>
                <CollapsibleTrigger className="flex w-full items-center justify-between">
                  <div className="flex items-center">
                    <span>Image Albums</span>
                    {isImageFoldersLoading && (
                      <Loader2 className="h-3 w-3 ml-2 animate-spin text-muted-foreground" />
                    )}
                  </div>
                  <ChevronDown className="h-4 w-4 transition-transform duration-200" 
                    style={{ 
                      transform: isImageFoldersOpen ? 'rotate(0deg)' : 'rotate(-90deg)' 
                    }}
                  />
                </CollapsibleTrigger>
              </SidebarGroupLabel>
              <CollapsibleContent>
                <SidebarGroupContent>
                  <SidebarMenu>
                    {/* Show All Images option */}
                    <SidebarMenuItem>
                      <Link href="/new-image" passHref legacyBehavior>
                        <SidebarMenuButton 
                          asChild
                          data-active={isImageFolderActive(null)}
                          className="data-[active=true]:bg-sidebar-accent data-[active=true]:text-sidebar-accent-foreground data-[active=true]:font-medium"
                        >
                          <a>
                            <ImageIcon
                              className="h-4 w-4 mr-2"
                              style={navColor(1) ? { color: navColor(1) } : undefined}
                            />
                            <span>All Images</span>
                          </a>
                        </SidebarMenuButton>
                      </Link>
                    </SidebarMenuItem>
                    
                    {/* Image Folder List */}
                    {isImageFoldersLoading ? (
                      renderFolderSkeletons()
                    ) : (
                      imageFolders.map((folder, index) => (
                        <motion.div
                          key={folder}
                          custom={index}
                          initial="hidden"
                          animate="visible"
                          variants={folderItemVariants}
                        >
                          <SidebarMenuItem>
                            <SidebarMenuButton 
                              asChild
                              data-active={isImageFolderActive(folder)}
                              className="data-[active=true]:bg-sidebar-accent data-[active=true]:text-sidebar-accent-foreground data-[active=true]:font-medium"
                              onClick={() => handleImageFolderClick(folder)}
                            >
                              <a>
                                <FolderIcon
                                  className="h-4 w-4 mr-2"
                                  style={
                                    navColor(index + 2)
                                      ? { color: navColor(index + 2) }
                                      : undefined
                                  }
                                />
                                <span>{folder.split('/').pop() || folder}</span>
                              </a>
                            </SidebarMenuButton>
                          </SidebarMenuItem>
                        </motion.div>
                      ))
                    )}
                  </SidebarMenu>
                </SidebarGroupContent>
              </CollapsibleContent>
            </SidebarGroup>
          </Collapsible>
        </div>

        {/* Manage Section */}
        <SidebarGroup>
          <SidebarGroupLabel className="group-data-[collapsible=icon]:hidden">Manage</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {manageItems.map((item, i) => {
                const active = pathname === item.url;
                // Continue the palette after the Create section so Manage
                // items don't repeat the same colors.
                const color = navColor(createItems.length + i);
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