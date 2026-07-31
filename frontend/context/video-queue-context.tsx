"use client";

import { createContext, useContext, useEffect, useState, useRef } from "react";
import { 
  VideoGenerationJob, 
  createVideoGenerationJob, 
  getVideoGenerationJob, 
  mapSettingsToApiRequest, 
  downloadThenUploadToGallery, 
  generateVideoFilename, 
  analyzeAndUpdateVideoMetadata, 
  createVideoGenerationWithAnalysis, 
  VideoGenerationWithAnalysisRequest,
  streamVideoGenerationWithAnalysis,
  VideoStreamEvent,
  moderationSafeRewrite,
} from "@/services/api";
import { toast } from "sonner";

// Global set to track which generation IDs have already been uploaded
// This survives component re-renders and ensures each generation is only uploaded once
const uploadedGenerations = new Set<string>();

// Global callback registry for refresh notifications
// Components can register callbacks to be notified when uploads complete
type RefreshCallback = () => void;
const refreshCallbacks: RefreshCallback[] = [];

function formatVideoCount(count: number): string {
  return `${count} video${count === 1 ? '' : 's'}`;
}

function formatAspectRatio(width?: number, height?: number): string {
  if (!width || !height) {
    return "unknown";
  }
  return `${width}x${height}`;
}

function formatDuration(seconds?: number): string {
  if (!seconds) {
    return "unknown";
  }
  return `${seconds}s`;
}

// Parse the backend's `failure_reason` (a Python-dict-like string such as
// "{'code': 'moderation_blocked', 'message': 'Your request was blocked...'}")
// into a user-friendly title + description. For moderation_blocked we include a
// concrete rewrite example so users know how to fix the prompt without leaving
// the app.
function formatFailureMessage(reason?: string | null): { title: string; description: string; code?: string } {
  const raw = (reason || "").trim();
  const codeMatch = raw.match(/'code'\s*:\s*'([^']+)'/) || raw.match(/"code"\s*:\s*"([^"]+)"/);
  const msgMatch  = raw.match(/'message'\s*:\s*'([^']+)'/) || raw.match(/"message"\s*:\s*"([^"]+)"/);
  const code = codeMatch?.[1];
  const message = msgMatch?.[1] || raw;

  // Sora rate limit — detect either the parsed code or the raw 429/rate_limit text
  const looksRateLimited =
    code === "rate_limit_exceeded" ||
    /rate.?limit/i.test(raw) ||
    /429\b/.test(raw) ||
    /too many requests/i.test(raw);
  if (looksRateLimited) {
    return {
      code: "rate_limit_exceeded",
      title: "Sora is rate-limited",
      description:
        "You're hitting the Sora deployment's per-minute quota. Wait ~30\u201360 seconds and try again. " +
        "If this happens often, increase the Sora deployment capacity in Azure AI Foundry.",
    };
  }

  if (code === "moderation_blocked") {
    return {
      code,
      title: "Prompt blocked by content moderation",
      description:
        "Sora blocks copyrighted or branded content (movies, characters, celebrities, logos). " +
        "Try describing what you want visually \u2014 colors, hair, clothing, setting \u2014 without naming the source. " +
        "Example: instead of \u201CElsa from Frozen\u201D, write \u201Ca princess with long platinum-blonde braided hair in a light blue dress\u201D.",
    };
  }
  return {
    code,
    title: "Video generation failed",
    description: message || "The video could not be generated. Please try again with a different prompt.",
  };
}

// Ask the backend LLM to rewrite a moderation-blocked prompt, then show a
// single toast with the rewritten prompt and "Use this prompt" + "Copy" actions.
// Isolated so an LLM failure doesn't affect queue state.
// Custom event so any Video prompt input (e.g., VideoOverlay) can auto-populate.
export const PROMPT_SUGGESTION_EVENT = "visionary-lab:set-video-prompt";

function dispatchPromptSuggestion(prompt: string) {
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent(PROMPT_SUGGESTION_EVENT, { detail: { prompt } })
    );
  }
}

// Track prompts that came from a moderation-safe rewrite. If one of those
// prompts fails moderation AGAIN, we escalate the toast copy instead of
// offering yet another rewrite loop. Module-level so it persists across
// re-renders within the same session.
const rewrittenPromptCache = new Set<string>();
function rememberRewrittenPrompt(prompt: string) {
  const key = (prompt || "").trim();
  if (key) rewrittenPromptCache.add(key);
}
function wasFromRewrite(prompt: string): boolean {
  const key = (prompt || "").trim();
  return key.length > 0 && rewrittenPromptCache.has(key);
}

// Marker error used when the queue context has already surfaced a toast for the
// underlying failure. Outer catches (e.g. in the page-level generate handler)
// can inspect `.handled` to avoid firing a duplicate generic toast.
export class HandledQueueError extends Error {
  handled = true as const;
  constructor(message: string) {
    super(message);
    this.name = "HandledQueueError";
  }
}

// Runs on moderation-blocked failures. Auto-calls the rewrite endpoint and
// shows a single toast with the safer prompt + "Use this prompt" / "Copy"
// actions. Best-effort: if the rewrite fails, fall back to a static tip.
async function handleModerationFailure(originalPrompt: string) {
  const workingToast = toast.loading(
    "Prompt was blocked. Suggesting a safer version…",
    {
      description:
        "Rewriting your prompt to avoid copyrighted or branded content.",
    }
  );
  try {
    const rewritten = (await moderationSafeRewrite(originalPrompt)).trim();
    if (!rewritten) throw new Error("empty rewrite");
    toast.success("Try this safer prompt instead", {
      id: workingToast,
      description: rewritten,
      duration: 30000,
      action: {
        label: "Use this prompt",
        onClick: () => {
          rememberRewrittenPrompt(rewritten);
          dispatchPromptSuggestion(rewritten);
          toast.success("Prompt updated \u2014 review and hit Generate", {
            duration: 4000,
          });
        },
      },
      cancel: {
        label: "Copy",
        onClick: () => {
          if (typeof navigator !== "undefined" && navigator.clipboard) {
            navigator.clipboard.writeText(rewritten).then(
              () => toast.success("Copied to clipboard", { duration: 3000 }),
              () =>
                toast.error("Could not copy to clipboard", { duration: 4000 })
            );
          }
        },
      },
    });
  } catch (err) {
    console.error("moderationSafeRewrite failed:", err);
    toast.error("Prompt blocked by content moderation", {
      id: workingToast,
      description:
        "Sora blocks copyrighted or branded content. Try describing the scene visually (colors, hair, clothing, setting) without naming the source.",
      duration: 15000,
    });
  }
}

// Central failure handler used by BOTH the polling path and the SSE stream
// error branch. Guarantees a single, well-formatted toast per failure and
// triggers the auto-rewrite for moderation blocks.
function handleGenerationFailure(
  failureReason: string | null | undefined,
  originalPrompt: string
) {
  const { title, description, code } = formatFailureMessage(failureReason);

  // Escalate: if the failing prompt was already produced by our rewrite,
  // don't offer another rewrite loop — tell the user the theme itself is
  // too tied to copyrighted content and to try a totally different scene.
  if (code === "moderation_blocked" && wasFromRewrite(originalPrompt)) {
    toast.error("Even the safer version was blocked", {
      description:
        "This scene may be too closely tied to copyrighted characters or brands. " +
        "Try a completely different subject \u2014 for example animals, nature, food, sports, or abstract art \u2014 while keeping the mood you want.",
      duration: 20000,
    });
    return;
  }

  if (code === "moderation_blocked" && originalPrompt) {
    // Auto-rewrite path replaces the generic error toast entirely.
    handleModerationFailure(originalPrompt);
    return;
  }
  toast.error(title, { description, duration: 15000 });
}

// Register a callback to be called when uploads complete
export function registerGalleryRefreshCallback(callback: RefreshCallback) {
  if (!refreshCallbacks.includes(callback)) {
    refreshCallbacks.push(callback);
    return true;
  }
  return false;
}

// Unregister a callback when component unmounts
export function unregisterGalleryRefreshCallback(callback: RefreshCallback) {
  const index = refreshCallbacks.indexOf(callback);
  if (index !== -1) {
    refreshCallbacks.splice(index, 1);
    return true;
  }
  return false;
}

// Trigger all registered callbacks
function notifyGalleryRefreshNeeded() {
  refreshCallbacks.forEach(callback => {
    try {
      callback();
    } catch (error) {
      console.error('Error in gallery refresh callback:', error);
    }
  });
}

export interface VideoQueueItem {
  id: string;
  prompt: string;
  status: "pending" | "processing" | "completed" | "failed";
  progress?: number;
  createdAt: Date;
  job?: VideoGenerationJob; // Add the actual job data
  uploadComplete?: boolean; // Flag to track when uploads are complete
  uploadStarted?: boolean; // Flag to track when uploads are starting
  analysisSettings?: {
    analyzeVideo: boolean;
  };
  folder?: string; // Store folder information directly in queue item
}

export interface VideoSettings {
  resolution: string;
  duration: number; // This will be parsed from string like "5s"
  aspectRatio: string; // Added aspectRatio
  fps?: number;
  brandsProtection?: string; // Add brand protection mode
  brandsList?: string[]; // Add list of brands to protect
  folder?: string; // Add folder information
  analyzeVideo?: boolean; // Add video analysis setting
  // NEW: Optional source images for Image+Text to Video
  sourceImages?: File[];
  // Sora 2 NEW: Cameo and Remix settings
  selectedCameo?: string | null;
  remixVideoId?: string | null;
}

interface VideoQueueContextType {
  queueItems: VideoQueueItem[];
  addToQueue: (prompt: string, settings?: VideoSettings) => Promise<string>; // Returns the ID
  updateQueueItemStatus: (id: string, status: VideoQueueItem["status"], progress?: number) => void;
  removeFromQueue: (id: string) => void;
  getQueueItem: (id: string) => VideoQueueItem | undefined;
}

const VideoQueueContext = createContext<VideoQueueContextType | undefined>(undefined);

export function VideoQueueProvider({ children }: { children: React.ReactNode }) {
  const [queueItems, setQueueItems] = useState<VideoQueueItem[]>([]);
  const [isClient, setIsClient] = useState(false);
  // Track active SSE streams so we can abort on completion/removal
  const streamCleanupRef = useRef<Record<string, () => void>>({});

  // This effect runs once on client-side to mark that we're now on the client
  useEffect(() => {
    setIsClient(true);
  }, []);

  // Poll for job status updates every 5 seconds
  useEffect(() => {
    // Skip processing during server-side rendering or before client hydration is complete
    if (!isClient) return;

    // Create a polling function to check job status
    const pollJobStatus = async () => {
      // Clone the queue items to avoid mutating state directly
      const updatedItems = [...queueItems];
      let hasUpdates = false;
      let uploadCompleted = false;

      // Check each item with a backend job
      for (let i = 0; i < updatedItems.length; i++) {
        const item = updatedItems[i];
        
        // Skip items without a job or already completed/failed
        if (!item.job || item.status === "completed" || item.status === "failed") {
          continue;
        }
        
        // Skip items where uploads are already in progress or complete
        if (item.uploadStarted || item.uploadComplete) {
          continue;
        }
        
        try {
          // Get latest job status from API
          const updatedJob = await getVideoGenerationJob(item.job.id);
          
          // Update job data
          updatedItems[i] = {
            ...item,
            job: updatedJob,
          };
          
          // Update status based on job status
          if (updatedJob.status === "succeeded") {
            updatedItems[i].status = "completed";
            updatedItems[i].progress = 100;
            hasUpdates = true;

            // Handle uploading multiple generations if they exist
            if (updatedJob.generations && updatedJob.generations.length > 0) {
              // Handle each generation
              const uploadPromises = updatedJob.generations
                .filter(generation => !uploadedGenerations.has(generation.id)) // Only process generations not already uploaded
                .map(async (generation: { id: string; prompt?: string }, index: number) => {
                  // Mark this generation as being processed to prevent duplicate uploads
                  uploadedGenerations.add(generation.id);
                  
                  const fileName = generateVideoFilename(generation.prompt || item.prompt, generation.id);
                  
                  // Define metadata for the uploaded asset
                  const metadata = {
                    prompt: generation.prompt || item.prompt,
                    sourceJobId: item.job?.id || "unknown_job",
                    generationId: generation.id,
                    variantIndex: (index + 1).toString(),
                    totalVariants: updatedJob.generations?.length.toString() || "0",
                    originalAspectRatio: formatAspectRatio(item.job?.width, item.job?.height),
                    originalDuration: formatDuration(item.job?.n_seconds),
                    folder: item.job?.metadata?.folder || "root"
                  };

                  // Pass folder information to the download/upload function if available
                  // Use folder from queue item first, then fall back to job metadata
                  const folder = item.folder || item.job?.metadata?.folder || undefined;
                  
                  try {
                    const { uploadResponse } = await downloadThenUploadToGallery(
                      generation.id, 
                      fileName, 
                      metadata, 
                      folder
                    );
                    
                    // Analyze the video if analysis is enabled for this queue item
                    const queueItem = queueItems.find(item => item.job?.id === updatedJob.id);
                    const analysisSettings = queueItem?.analysisSettings;
                    
                    if (analysisSettings?.analyzeVideo) {
                      try {
                        // Wait 10 seconds for Azure Blob Storage to propagate the uploaded video
                        await new Promise(resolve => setTimeout(resolve, 10000));
                        
                        // Use the actual blob name returned from upload (includes folder path)
                        const blobName = uploadResponse?.blob_name || fileName;
                        await analyzeAndUpdateVideoMetadata(blobName);
                        
                        // Don't show individual analysis toasts - we'll show a consolidated one later
                                              } catch (analysisError) {
                          console.error(`Video analysis failed for ${generation.id}:`, analysisError);
                          // Don't show individual analysis error toasts - log the error for debugging
                        }
                    }
                    
                    return true;
                  } catch (error) {
                    console.error(`Error uploading generation ${generation.id}:`, error);
                    return false;
                  }
                });
              
              if (uploadPromises.length > 0) {
                // Use a loading toast that transforms into success/error
                const uploadToastId = toast.loading(`Uploading ${formatVideoCount(uploadPromises.length)} to gallery...`);
                
                try {
                  // Wait for all uploads to complete
                  const results = await Promise.all(uploadPromises);
                  const successCount = results.filter(Boolean).length;
                  
                  if (successCount > 0) {
                    // Check if analysis was enabled for this job
                    const queueItem = queueItems.find(item => item.job?.id === updatedJob.id);
                    const analysisEnabled = queueItem?.analysisSettings?.analyzeVideo;
                    
                    const description = analysisEnabled 
                      ? `${formatVideoCount(successCount)} uploaded with AI analysis`
                      : `${formatVideoCount(successCount)} ready in your gallery`;
                    
                    toast.success(`Videos uploaded successfully`, {
                      id: uploadToastId,
                      description,
                      duration: 5000
                    });
                    
                    // Set flag to notify galleries about new content
                    uploadCompleted = true;
                  }
                  
                  if (successCount < uploadPromises.length) {
                    const failedCount = uploadPromises.length - successCount;
                    toast.error(`${formatVideoCount(failedCount)} failed to upload`, {
                      id: uploadToastId
                    });
                  }
                } catch (uploadError) {
                  console.error(`Error handling uploads:`, uploadError);
                  toast.error(`Some videos failed to upload`, {
                    id: uploadToastId
                  });
                }
              } else {
                console.log(`All generations for job ${updatedJob.id} were already uploaded`);
              }
              
              // Mark this item with a special "uploaded" flag so the UI knows everything is ready
              updatedItems[i].uploadComplete = true;
            } else {
              console.log(`Job ${updatedJob.id} completed but no generations were found`);
              toast.info(`Job completed but no videos were generated.`);
              updatedItems[i].uploadComplete = true;
            }

          } else if (updatedJob.status === "failed") {
            updatedItems[i].status = "failed";
            hasUpdates = true;
            const originalPrompt =
              (updatedJob.prompt && updatedJob.prompt.trim()) ||
              (item.prompt && item.prompt.trim()) ||
              "";
            handleGenerationFailure(updatedJob.failure_reason, originalPrompt);
          } else if (updatedJob.status === "processing" || updatedJob.status === "queued") {
            if (item.status !== "processing") {
              updatedItems[i].status = "processing";
              hasUpdates = true;
            }
            
            // Use real progress from Sora 2 API when available, otherwise estimate
            if (updatedJob.progress != null) {
              const realProgress = Math.min(95, updatedJob.progress);
              if (Math.abs((item.progress || 0) - realProgress) > 2) {
                updatedItems[i].progress = realProgress;
                hasUpdates = true;
              }
            } else if (updatedJob.created_at) {
              const elapsedSeconds = (Date.now() / 1000) - updatedJob.created_at;
              const estimatedProgress = Math.min(95, (elapsedSeconds / 120) * 100);
              
              if (Math.abs((item.progress || 0) - estimatedProgress) > 5) {
                updatedItems[i].progress = estimatedProgress;
                hasUpdates = true;
              }
            }
          }
        } catch (error) {
          console.error(`Error polling job ${item.job.id}:`, error);
          
          // Don't mark as failed immediately for timeout/connection errors
          // Instead, just skip this update and try again on the next poll
          
          // Only update status to failed if it's a clear API failure (not a connection issue)
          if (error instanceof Error && 
              !(error.message.includes("timeout") || 
                error.message.includes("network") || 
                error.message.includes("Connection") ||
                error.message.includes("Max retries exceeded"))) {
            updatedItems[i].status = "failed";
            hasUpdates = true;
          }
        }
      }
      
      // Update state if there were any changes
      if (hasUpdates) {
        setQueueItems(updatedItems);
      }
      
      // If any uploads were completed, notify all registered gallery components
      if (uploadCompleted) {
        // Slight delay to ensure uploads are fully registered in the backend
        setTimeout(() => {
          notifyGalleryRefreshNeeded();
        }, 500);
      }
    };
    
    // Poll every 5 seconds instead of 2
    const intervalId = setInterval(pollJobStatus, 5000);
    
    // Run once immediately
    pollJobStatus();
    
    return () => clearInterval(intervalId);
  }, [queueItems, isClient]);

  const addToQueue = async (prompt: string, settings?: VideoSettings): Promise<string> => {
    // Ensure we're on the client side
    if (!isClient) return "";
    
    try {
      // Generate a temporary local ID for immediate UI feedback
      const tempId = `temp-${Date.now()}`;
      
      // Create initial queue item
      const newItem: VideoQueueItem = {
        id: tempId,
        prompt,
        status: "pending",
        createdAt: new Date(),
        analysisSettings: settings ? {
          analyzeVideo: settings.analyzeVideo || false
        } : undefined,
        folder: settings?.folder, // Store folder directly in queue item
      };
      
      // Update queue with pending item
      setQueueItems(prev => [...prev, newItem]);
      
      // If settings are provided, create a real backend job
      if (settings) {
        const apiRequest = mapSettingsToApiRequest({
          prompt,
          resolution: settings.resolution,
          duration: settings.duration.toString(), // Convert number to string
          aspectRatio: settings.aspectRatio, // Pass aspectRatio
          fps: settings.fps,
          // Sora 2 NEW: Pass cameo and remix settings
          selectedCameo: settings.selectedCameo,
          remixVideoId: settings.remixVideoId
        });
        
        // Add folder information to the job metadata
        const jobMetadata: Record<string, string> = {};
        if (settings.folder) {
          jobMetadata.folder = settings.folder;
        }
        if (settings.brandsProtection && settings.brandsProtection !== "off") {
          jobMetadata.brandsProtection = settings.brandsProtection;
        }
        if (settings.brandsList && settings.brandsList.length > 0) {
          jobMetadata.brandsList = JSON.stringify(settings.brandsList);
        }
        if (settings.analyzeVideo !== undefined) {
          jobMetadata.analyzeVideo = settings.analyzeVideo.toString();
        }
        
        // Prefer SSE streaming endpoint when analysis is requested (real-time progress)
        if (settings.analyzeVideo) {
          // Use the SSE streaming endpoint for real-time progress updates
          const unifiedRequest: VideoGenerationWithAnalysisRequest = {
            ...apiRequest,
            analyze_video: true,
            metadata: jobMetadata,
            // Pass images for image+text flow
            sourceImages: settings.sourceImages,
          };
          
          return new Promise<string>((resolve, reject) => {
            let jobId: string | null = null;
            
            // Start SSE stream
            const cleanup = streamVideoGenerationWithAnalysis(unifiedRequest, (event: VideoStreamEvent) => {
              switch (event.type) {
                case 'status':
                  // Initial status update
                  setQueueItems(prev => 
                    prev.map(item => 
                      item.id === tempId
                        ? { ...item, status: "processing" as const, uploadStarted: true }
                        : item
                    )
                  );
                  break;
                  
                case 'created':
                  // Job was created - update with real job ID
                  jobId = event.job_id;
                  // Move cleanup from tempId to real job id for later cancellation
                  streamCleanupRef.current[event.job_id] = streamCleanupRef.current[tempId] || cleanup;
                  delete streamCleanupRef.current[tempId];
            setQueueItems(prev => 
              prev.map(item => 
                item.id === tempId
                        ? { 
                            ...item, 
                            id: event.job_id,
                            status: "processing" as const,
                            job: { id: event.job_id, status: event.status } as VideoGenerationJob,
                            uploadStarted: true,
                          }
                        : item
                    )
                  );
                  break;
                  
                case 'progress':
                  // Progress update from Sora API
                  setQueueItems(prev => 
                    prev.map(item => 
                      (item.id === tempId || item.id === jobId)
                        ? { 
                            ...item, 
                            status: "processing" as const,
                            progress: event.progress,
                            uploadStarted: true,
                          }
                        : item
                    )
                  );
                  break;
                  
                case 'processing':
                  // Post-generation processing (downloading, analyzing, uploading)
                  const stepMessages: Record<string, string> = {
                    'downloading': 'Downloading video...',
                    'analyzing': 'Analyzing with AI...',
                    'uploading': 'Uploading to gallery...',
                  };
                  // Could show toast or update UI with current step
                  console.log(`Processing step: ${event.step}`);
                  break;
                  
                case 'complete':
                  // All done!
                  const job = event.job;
                  setQueueItems(prev => 
                    prev.map(item => 
                      (item.id === tempId || item.id === jobId)
                  ? { 
                      ...item, 
                      id: job.id, 
                            job: job,
                            status: "completed" as const,
                      progress: 100,
                            uploadComplete: true,
                            uploadStarted: true,
                            folder: item.folder,
                    }
                  : item
              )
            );
            
                  // Abort stream and cleanup
                  (streamCleanupRef.current[jobId || tempId] || cleanup)();
                  delete streamCleanupRef.current[jobId || tempId];
            
                  // Show success toast
                  const hasAnalysis = event.analysis_results && event.analysis_results.length > 0;
                  toast.success('Video generated successfully', {
                    description: hasAnalysis 
                      ? 'Video uploaded with AI analysis'
                      : 'Video ready in your gallery',
                    duration: 5000,
                  });
                  
                  // Notify gallery to refresh
                  setTimeout(() => {
                    notifyGalleryRefreshNeeded();
                  }, 500);
                  
                  resolve(job.id);
                  break;
                  
                case 'error':
                  // Error occurred
                  console.error('SSE stream error:', event.error);
            setQueueItems(prev => 
              prev.map(item => 
                      (item.id === tempId || item.id === jobId)
                        ? { ...item, status: "failed" as const }
                  : item
              )
            );
                  // Abort stream and cleanup
                  (streamCleanupRef.current[jobId || tempId] || cleanup)();
                  delete streamCleanupRef.current[jobId || tempId];
                  // Show ONE well-formatted toast (auto-rewrite on moderation)
                  handleGenerationFailure(event.error, prompt);
                  // Reject with a marker so the outer catch in the page-level
                  // generate handler doesn't fire a duplicate generic toast.
                  reject(new HandledQueueError(event.error || "Video generation failed"));
                  break;
          }
            });
            
            // Store cleanup function in case we need to abort
            streamCleanupRef.current[tempId] = cleanup;
          });
        } else {
          // Use traditional endpoint for non-analysis jobs
          const job = await createVideoGenerationJob({
            ...apiRequest,
            metadata: jobMetadata,
            // Pass images and folder for compatibility
            sourceImages: settings.sourceImages,
            folder_path: settings.folder,
          });
          
          // Update the queue item with the real job ID and data
          setQueueItems(prev => 
            prev.map(item => 
              item.id === tempId
                ? { ...item, id: job.id, job, folder: item.folder } // Preserve folder information
                : item
            )
          );
          
                     return job.id;
         }
      }
      
      return tempId;
    } catch (error) {
      console.error("Error adding to queue:", error);
      return "";
    }
  };

  const updateQueueItemStatus = (
    id: string, 
    status: VideoQueueItem["status"], 
    progress?: number
  ) => {
    setQueueItems(prev => 
      prev.map(item => {
        if (item.id !== id) {
          return item;
        }
        if (progress === undefined) {
          return { ...item, status };
        }
        return { ...item, status, progress };
      })
    );
  };

  const removeFromQueue = (id: string) => {
    const cleanup = streamCleanupRef.current[id];
    if (cleanup) {
      cleanup();
      delete streamCleanupRef.current[id];
    }
    setQueueItems(prev => prev.filter(item => item.id !== id));
  };
  
  const getQueueItem = (id: string) => {
    return queueItems.find(item => item.id === id);
  };

  return (
    <VideoQueueContext.Provider
      value={{
        queueItems,
        addToQueue,
        updateQueueItemStatus,
        removeFromQueue,
        getQueueItem,
      }}
    >
      {children}
    </VideoQueueContext.Provider>
  );
}

export function useVideoQueue() {
  const context = useContext(VideoQueueContext);
  if (context === undefined) {
    throw new Error("useVideoQueue must be used within a VideoQueueProvider");
  }
  return context;
} 
