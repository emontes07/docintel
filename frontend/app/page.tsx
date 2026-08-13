"use client";

import { PageHeader } from "@/components/page-header";

export default function HomePage() {
  return (
    <div className="flex flex-col h-full">
      <PageHeader title="Document Intelligence" />
      <div className="flex flex-1 items-center justify-center p-8">
        <p className="text-sm text-muted-foreground">
          Document extraction is not wired up yet.
        </p>
      </div>
    </div>
  );
}
