"use client";

import { useEffect, useRef, useState } from "react";
import { Download, FileCheck2, LoaderCircle, Play, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EnrichmentReview, ReviewMachine } from "@/components/enrichment-review";
import { executionLabel, sourceLabel, stagePresentation } from "@/utils/pilot-presentation";

type Source = { source_id: string; kind: string; location: string; availability: string; enabled: boolean; replay_available: boolean };
type Catalog = { product: { item_id: string; mpn: string; vendor: string }; sources: Source[]; live_configured: boolean; analysis_configured: boolean; budget: { analysis_used: number; analysis_limit: number; inference_used: number; inference_limit: number } };
type Machine = ReviewMachine;
type Run = { id: string; state: string; created_at: string; request: { source_id: string; mode: string }; stages: Record<string, { status: string; explanation?: string; origin?: string; failure?: { explanation: string } | null }>; source: { kind: string; location: string; sha256: string | null; etag: string | null; failed_stage?: string; explanation?: string; stages?: { stage: string; http_status?: number }[] } | null; parsing: { status: string; origin: string; parsed_at: string } | null; machine_result: Machine | null; reviewed_result: Machine | null };
type Summary = Pick<Run, "id" | "state" | "created_at" | "request">;

async function api<Value>(path: string, body?: unknown): Promise<Value> {
  const response = await fetch(`/api/pilot/${path}`, {
    method: body ? "POST" : "GET", cache: "no-store",
    headers: { "Content-Type": "application/json", "X-DocIntel-Local": "1" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || "Local request failed");
  return value;
}

const inputClass = "w-full min-w-0 rounded-md border bg-background px-3 py-2 text-sm";

export default function PilotPage() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [runs, setRuns] = useState<Summary[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [sourceId, setSourceId] = useState("");
  const [mode, setMode] = useState("offline_replay");
  const [fresh, setFresh] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [repeat, setRepeat] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [exportedFile, setExportedFile] = useState<{ runId: string; location: string } | null>(null);
  const [exporting, setExporting] = useState(false);
  const submitting = useRef(false);
  const active = run?.state === "queued" || run?.state === "running";
  const activeRunId = run?.id;
  const selectedSource = catalog?.sources.find(source => source.source_id === sourceId);

  async function refresh() {
    try {
      const [configuration, history] = await Promise.all([api<Catalog>("catalog"), api<Summary[]>("runs")]);
      setCatalog(configuration); setRuns(history);
      setSourceId(current => current || configuration.sources.find(source => source.kind === "local")?.source_id || "");
      if (history.length) setRun(await api<Run>(`runs/${history[0].id}`));
      setError("");
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Local backend unavailable"); }
  }
  useEffect(() => { void refresh(); }, []);
  useEffect(() => {
    if (!active || !activeRunId) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const updated = await api<Run>(`runs/${activeRunId}`);
        if (!cancelled) {
          setRun(updated);
          if (!["queued", "running"].includes(updated.state)) {
            setRuns(await api<Summary[]>("runs")); setCatalog(await api<Catalog>("catalog"));
          }
        }
      } catch (failure) { if (!cancelled) setError(failure instanceof Error ? failure.message : "Status unavailable"); }
    }, 1200);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [active, activeRunId]);

  async function start() {
    if (submitting.current || active) return;
    submitting.current = true; setBusy(true); setError("");
    try {
      const operation = { source_id: sourceId, mode, parse_mode: fresh && mode === "live_inference" ? "fresh" : "reuse", confirm_live: mode === "live_inference" && confirmed, confirm_repeat: repeat };
      const signature = JSON.stringify(operation);
      const pending = JSON.parse(sessionStorage.getItem("docintel-pilot-start") || "null") as { signature: string; id: string } | null;
      const requestId = pending?.signature === signature ? pending.id : crypto.randomUUID();
      sessionStorage.setItem("docintel-pilot-start", JSON.stringify({ signature, id: requestId }));
      const result = await api<{ id: string; created: boolean }>("runs", { ...operation, request_id: requestId });
      setRun(await api<Run>(`runs/${result.id}`));
      setRuns(await api<Summary[]>("runs"));
      sessionStorage.removeItem("docintel-pilot-start");
      setConfirmed(false); setRepeat(false);
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Run failed"); }
    finally { setBusy(false); submitting.current = false; }
  }

  async function download() {
    if (!run) return;
    setExporting(true);
    try {
      const exported = await api<{ location: string }>(`runs/${run.id}/export`, {});
      setExportedFile({ runId: run.id, location: exported.location });
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Export failed"); }
    finally { setExporting(false); }
  }

  return <div className="mx-auto max-w-6xl space-y-6 px-4 py-6 md:px-8">
    <header className="flex flex-wrap items-start justify-between gap-3 border-b pb-4">
      <div><div className="flex items-center gap-2"><FileCheck2 className="text-emerald-600" size={24} /><h1 className="text-2xl font-semibold">Product Review</h1></div><p className="mt-2 text-sm text-muted-foreground">Local prototype · Private workspace · No master-data writes</p></div>
      <Button variant="outline" size="icon" title="Refresh runs" aria-label="Refresh runs" onClick={refresh}><RefreshCw size={16} /></Button>
    </header>
    {error && <p role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900">{error}</p>}
    {!catalog ? <p role="status">Waiting for local pilot configuration.</p> : <>
      <section className="space-y-4" aria-label="Start a new run">
        <h2 className="text-lg font-semibold">Start a new run</h2>
        <div><h3 className="font-semibold">{catalog.product.mpn}</h3><p className="text-sm break-words">{catalog.product.vendor} · {catalog.product.item_id}</p></div>
        <div className="grid gap-4 md:grid-cols-3">
          <label className="text-sm space-y-1">Pilot<select className={inputClass} aria-label="Pilot"><option>Ford angle valve · Two attributes</option></select></label>
          <label className="text-sm space-y-1">Source<select className={inputClass} aria-label="Source" disabled={busy || active} value={sourceId} onChange={event => { setSourceId(event.target.value); setConfirmed(false); if (!catalog.sources.find(source => source.source_id === event.target.value)?.replay_available) setMode("live_inference"); }}>
            {catalog.sources.map(source => <option value={source.source_id} key={source.source_id}>{source.kind === "local" ? "Local file (approved PDF)" : "SharePoint (download blocked)"}</option>)}
          </select></label>
          <label className="text-sm space-y-1">Execution<select className={inputClass} aria-label="Execution" disabled={busy || active} value={mode} onChange={event => { setMode(event.target.value); setConfirmed(false); setFresh(false); }}>
            <option value="offline_replay" disabled={!selectedSource?.replay_available}>Replay prior pilot</option><option value="live_inference" disabled={!catalog.live_configured}>Live inference</option>
          </select></label>
        </div>
        <p className="text-sm text-muted-foreground break-words">{selectedSource?.availability}</p>
        {selectedSource && <p className="text-sm break-all">{sourceLabel(selectedSource.location).filename}</p>}
        <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">Configured source location</summary><p className="mt-2 break-all">{selectedSource?.location}</p></details>
        {mode === "live_inference" && <div className="space-y-3 border-l-2 border-amber-500 pl-4 text-sm">
          <label className="flex items-start gap-2"><input type="checkbox" disabled={busy || active || !catalog.analysis_configured} checked={fresh} onChange={event => setFresh(event.target.checked)} />Submit a fresh Document Intelligence analysis</label>
          <label className="flex items-start gap-2"><input type="checkbox" disabled={busy || active} checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />Confirm live Azure execution for this product and selected source</label>
          <p className="text-muted-foreground">Analysis budget: {catalog.budget.analysis_used}/{catalog.budget.analysis_limit} · Inference budget: {catalog.budget.inference_used}/{catalog.budget.inference_limit}</p>
        </div>}
        <div className="flex flex-wrap items-center gap-4">
          <Button onClick={start} disabled={busy || active || !sourceId || !selectedSource?.enabled || (mode === "live_inference" && !confirmed)}>{busy || active ? <LoaderCircle size={16} className="animate-spin" /> : <Play size={16} />}{mode === "offline_replay" ? "Run replay" : "Run live"}</Button>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={busy || active} checked={repeat} onChange={event => setRepeat(event.target.checked)} />Create another run even if one already exists</label>
        </div>
      </section>
      <section className="border-t pt-5 space-y-4" aria-label="Review a saved run">
        <h2 className="text-lg font-semibold">Review a saved run</h2>
        <div className="flex flex-wrap items-end gap-3 justify-between"><label className="text-sm flex-1 min-w-0 max-w-xl">Run history<select className={inputClass} aria-label="Run history" value={run?.id || ""} disabled={busy || active} onChange={async event => { try { setRun(await api<Run>(`runs/${event.target.value}`)); } catch { setError("Could not load run"); } }}><option value="" disabled>No run selected</option>{runs.map(item => <option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString()} · {executionLabel(item.request.mode)} · {item.state}</option>)}</select></label><Button variant="outline" onClick={download} disabled={!run || active || exporting}><Download size={16} />Export review JSON</Button></div>
        {exportedFile?.runId === run?.id && exportedFile && <div className="text-sm"><p role="status">Export saved privately.</p><details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">Export location</summary><p className="mt-2 break-all">{exportedFile.location}</p></details></div>}
        {run && <>
          <div className="flex flex-wrap gap-3 text-sm" data-testid="saved-run-provenance"><strong className="capitalize">{run.state}</strong><span>Recorded execution: {executionLabel(run.request.mode)}</span><span>Source: {run.source?.kind || run.request.source_id}</span></div>
          <ol className="grid grid-cols-2 gap-3 md:grid-cols-5" aria-label="Run stages">{Object.entries(run.stages).map(([name, stage]) => {
            const presentation = stagePresentation(name, stage.status, run.parsing);
            return <li key={name} className={`border-t-2 pt-2 text-sm min-w-0 ${stage.status === "failed" ? "border-red-500" : presentation.successful ? "border-emerald-600" : "border-border"}`}><p className="capitalize font-medium">{name}</p><p className="break-words">Outcome: {presentation.outcome}</p>{presentation.method && <p className="mt-1 text-muted-foreground break-words">Method: {presentation.method}</p>}{name === "parsing" && run.parsing?.origin === "imported_verified_pilot" && <p className="mt-1 text-muted-foreground">Prior verified pilot</p>}{stage.explanation && <p className="mt-2 text-red-600 break-words">{stage.explanation}</p>}{stage.failure && <p className="mt-2 text-red-600 break-words">{stage.failure.explanation}</p>}</li>;
          })}</ol>
          {run.source?.failed_stage && <p role="alert" className="text-sm text-red-600">Source failed at {run.source.failed_stage}. {run.source.stages?.map(stage => `${stage.stage}: ${stage.http_status ?? "local"}`).join("; ")}. {run.source.explanation} No local fallback.</p>}
          {run.source && <p className="text-sm break-all">Source file: {sourceLabel(run.source.location).filename}</p>}
          <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">Recorded provenance and full locations</summary><dl className="mt-2 space-y-1 break-all"><dt>Run ID</dt><dd>{run.id}</dd><dt>Source location</dt><dd>{run.source?.location || "Not recorded"}</dd><dt>Content SHA-256</dt><dd>{run.source?.sha256 || "Not established"}</dd><dt>ETag (separate from hash)</dt><dd>{run.source?.etag || "Not recorded"}</dd><dt>Recorded parsing status</dt><dd>{run.parsing?.status || "Not recorded"}</dd><dt>Parse origin</dt><dd>{run.parsing?.origin || "Not recorded"}</dd><dt>Parsed at</dt><dd>{run.parsing?.parsed_at || "Not recorded"}</dd></dl></details>
        </>}
      {run?.reviewed_result && <EnrichmentReview key={run.id} result={run.reviewed_result} identity="locally_entered_unverified" onReview={run.state === "completed" ? async value => { setRun(await api<Run>(`runs/${run.id}/reviews`, value)); } : undefined} />}
      </section>
    </>}
    <footer className="border-t pt-4 text-xs text-muted-foreground">Single-user local prototype. Reviewer identity is not authenticated. Dependency reproducibility and production access controls remain unresolved. Not ready for shared use or deployment.</footer>
  </div>;
}