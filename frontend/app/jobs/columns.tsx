"use client"

import { useState } from "react"
import { Check, X, RefreshCw, Clock, ArrowUpDown, Save } from "lucide-react"
import { ColumnDef } from "@tanstack/react-table"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { VideoJob } from "@/types/jobs"
import { finalizeVideoJob } from "@/services/api"

// Helper function to format timestamps from the API (which are in seconds)
function formatApiTimestamp(timestamp?: number): string {
  if (!timestamp) return "N/A"
  
  // Convert seconds to milliseconds for JavaScript Date
  const date = new Date(timestamp * 1000)
  
  // Format date: YYYY-MM-DD HH:MM
  return date.toLocaleString()
}

export const columns: ColumnDef<VideoJob>[] = [
  {
    accessorKey: "id",
    header: "ID",
    cell: ({ row }) => <div className="max-w-[100px] truncate">{row.getValue("id")}</div>,
    enableSorting: false,
  },
  {
    accessorKey: "prompt",
    header: ({ column }) => {
      return (
        <Button
          variant="ghost"
          onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
          className="p-0 hover:bg-transparent"
        >
          Prompt
          <ArrowUpDown className="ml-2 h-4 w-4" />
        </Button>
      )
    },
    cell: ({ row }) => {
      return (
        <div className="max-w-[500px] truncate font-medium">
          {row.getValue("prompt")}
        </div>
      )
    },
  },
  {
    accessorKey: "status",
    header: ({ column }) => {
      return (
        <Button
          variant="ghost"
          onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
          className="p-0 hover:bg-transparent"
        >
          Status
          <ArrowUpDown className="ml-2 h-4 w-4" />
        </Button>
      )
    },
    cell: ({ row }) => {
      const status = row.getValue("status") as string

      return (
        <div className="flex items-center">
          {(status === "pending" || status === "queued") && <Clock className="mr-2 h-4 w-4 text-muted-foreground" />}
          {(status === "in_progress" || status === "processing" || status === "preprocessing") && <RefreshCw className="mr-2 h-4 w-4 text-blue-500 animate-spin" />}
          {(status === "succeeded" || status === "completed") && <Check className="mr-2 h-4 w-4 text-green-500" />}
          {status === "failed" && <X className="mr-2 h-4 w-4 text-red-500" />}
          <Badge
            variant={
              (status === "pending" || status === "queued")
                ? "outline"
                : (status === "in_progress" || status === "processing" || status === "preprocessing")
                ? "secondary"
                : (status === "succeeded" || status === "completed")
                ? "success"
                : "destructive"
            }
          >
            {(status === "pending" || status === "queued") && "Pending"}
            {(status === "in_progress" || status === "processing" || status === "preprocessing") && "In Progress"}
            {(status === "succeeded" || status === "completed") && "Completed"}
            {status === "failed" && "Failed"}
            {!["pending", "queued", "in_progress", "processing", "preprocessing", "succeeded", "completed", "failed"].includes(status) && status}
          </Badge>
        </div>
      )
    },
  },
  {
    accessorKey: "createdAt",
    header: ({ column }) => {
      return (
        <Button
          variant="ghost"
          onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
          className="p-0 hover:bg-transparent"
        >
          Created
          <ArrowUpDown className="ml-2 h-4 w-4" />
        </Button>
      )
    },
    cell: ({ row }) => {
      const timestamp = row.getValue("createdAt") as number
      return <div>{formatApiTimestamp(timestamp)}</div>
    },
  },
  {
    accessorKey: "updatedAt",
    header: ({ column }) => {
      return (
        <Button
          variant="ghost"
          onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
          className="p-0 hover:bg-transparent"
        >
          Updated
          <ArrowUpDown className="ml-2 h-4 w-4" />
        </Button>
      )
    },
    cell: ({ row }) => {
      const timestamp = row.getValue("updatedAt") as number
      return <div>{formatApiTimestamp(timestamp)}</div>
    },
  },
  {
    id: "actions",
    header: "",
    enableSorting: false,
    cell: ({ row }) => {
      const status = row.getValue("status") as string
      const jobId = row.getValue("id") as string
      const isCompleted = status === "succeeded" || status === "completed"
      if (!isCompleted) return null
      return <FinalizeJobButton jobId={jobId} />
    },
  },
]

// Inline button used in the actions column. Server-finalizes a completed Sora
// job \u2014 downloads the video to blob + writes Cosmos metadata \u2014 so it
// shows up in the gallery even if the client-side polling never ran.
function FinalizeJobButton({ jobId }: { jobId: string }) {
  const [busy, setBusy] = useState(false)
  const onClick = async () => {
    if (busy) return
    setBusy(true)
    const workingId = toast.loading("Saving video to gallery\u2026")
    try {
      const res = await finalizeVideoJob(jobId)
      const gens = res.generations || []
      if (gens.length === 0) {
        toast.error("Nothing to save", {
          id: workingId,
          description: res.message || "Job is not complete yet.",
        })
      } else if (gens.every((g) => g.already_finalized)) {
        toast.success("Already in gallery", {
          id: workingId,
          description: "This video is already saved.",
        })
      } else {
        toast.success("Saved to gallery", {
          id: workingId,
          description: `${gens.length} video${gens.length === 1 ? "" : "s"} added.`,
        })
      }
    } catch (err) {
      toast.error("Could not save video", {
        id: workingId,
        description: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setBusy(false)
    }
  }
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={onClick}
      disabled={busy}
      className="h-7 px-2"
      title="Save this video to the gallery"
    >
      <Save className={"h-4 w-4 mr-1 " + (busy ? "animate-pulse" : "")} />
      {busy ? "Saving\u2026" : "Save to gallery"}
    </Button>
  )
}