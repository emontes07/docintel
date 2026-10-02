"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { signIn } from "next-auth/react";
import { CheckCircle2, ChevronLeft, ChevronRight, Download, FileSpreadsheet, LoaderCircle, Play, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

type Item = { item_key: string; row: number; original: Record<string, string>; state: string; errors: string[]; warnings: string[]; pending_review: boolean; error?: string };
type Batch = { id: string; state: string; valid: boolean; product_count: number; mode?: string; created_at: string; progress?: { finished: number; unresolved: number; failed: number } };
type Catalog = { identity: string; sources: { source_id: string; reference: string; kind: string; product_ids: string[] }[] };
const inputClass = "w-full min-w-0 rounded-md border bg-background px-3 py-2 text-sm";

async function api<Value>(path: string, body?: object | FormData): Promise<Value> {
  const response = await fetch(`/api/batches${path}`, { method: body ? "POST" : "GET", cache: "no-store", ...(body instanceof FormData ? { body } : body ? { body: JSON.stringify(body), headers: { "Content-Type": "application/json" } } : {}) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || "Batch request failed");
  return value;
}

export default function BatchesPage() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [history, setHistory] = useState<Batch[]>([]);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [manifest, setManifest] = useState<File | null>(null);
  const [attributes, setAttributes] = useState<File | null>(null);
  const [reference, setReference] = useState("");
  const [mode, setMode] = useState("evidence_only");
  const [confirmed, setConfirmed] = useState(false);
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const selection = useRef(0);

  async function historyRefresh() {
    try {
      const [configuration, batches] = await Promise.all([api<Catalog>("/catalog"), api<Batch[]>("")]);
      setCatalog(configuration); setHistory(batches); setError("");
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Batch configuration unavailable"); }
  }
  useEffect(() => { void historyRefresh(); }, []);
  const batchId = batch?.id;
  const active = batch?.state === "queued" || batch?.state === "running";
  useEffect(() => {
    if (!batchId) return;
    const generation = ++selection.current;
    let cancelled = false;
    async function refresh() {
      try {
        const [record, result] = await Promise.all([api<Batch>(`/${batchId}`), api<{ items: Item[]; total: number }>(`/${batchId}/items?offset=${page * 50}&limit=50&view=${filter}`)]);
        if (!cancelled && selection.current === generation) { setBatch(record); setItems(result.items); setTotal(result.total); }
      } catch (failure) { if (!cancelled) setError(failure instanceof Error ? failure.message : "Batch status unavailable"); }
    }
    void refresh();
    const timer = active ? window.setInterval(refresh, 10000) : undefined;
    return () => { cancelled = true; if (timer) window.clearInterval(timer); };
  }, [batchId, page, filter, active]);

  async function validate(event: React.FormEvent) {
    event.preventDefault();
    if (!manifest || !attributes || submitting.current) return;
    submitting.current = true; setBusy(true); setError("");
    try {
      const body = new FormData(); body.set("manifest", manifest); body.set("attributes", attributes); body.set("attribute_reference", reference);
      const result = await api<Batch>("/validate", body);
      setBatch(result); setItems([]); setPage(0); setFilter("all"); setConfirmed(false);
      await historyRefresh();
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Workbook validation failed"); }
    finally { submitting.current = false; setBusy(false); }
  }

  async function submit() {
    if (!batch || submitting.current) return;
    submitting.current = true; setBusy(true); setError("");
    try {
      const key = `docintel-batch-submit:${batch.id}:${mode}`;
      const requestId = sessionStorage.getItem(key) || crypto.randomUUID();
      sessionStorage.setItem(key, requestId);
      setBatch(await api<Batch>(`/${batch.id}/submit`, { request_id: requestId, mode, confirm_live: confirmed }));
      setConfirmed(false);
      await historyRefresh();
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Batch submission failed"); }
    finally { submitting.current = false; setBusy(false); }
  }

  return <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-8"><header className="flex flex-wrap justify-between gap-3 border-b pb-4"><div className="flex items-center gap-3"><FileSpreadsheet className="text-emerald-600" /><h1 className="text-2xl font-semibold">Batch Enrichment</h1></div><div className="flex gap-2"><Button variant="outline" onClick={() => signIn("microsoft-entra-id")}>Sign in</Button><Button size="icon" variant="outline" title="Refresh batches" aria-label="Refresh batches" onClick={historyRefresh}><RefreshCw size={16} /></Button></div></header>
    {error && <p role="alert" className="border-l-2 border-red-600 pl-3 text-sm text-red-700 dark:text-red-400">{error}</p>}
    <section className="space-y-4" aria-label="Workbook intake"><h2 className="text-lg font-semibold">Validate workbooks</h2><form onSubmit={validate} className="space-y-4"><div className="grid gap-4 md:grid-cols-3"><label className="text-sm space-y-2">Product manifest<input type="file" accept=".xlsx" required className={inputClass} onChange={event => setManifest(event.target.files?.[0] || null)} /></label><label className="text-sm space-y-2">Attribute definitions<input type="file" accept=".xlsx" required className={inputClass} onChange={event => { const file = event.target.files?.[0] || null; setAttributes(file); setReference(file?.name || ""); }} /></label><label className="text-sm space-y-2">Manifest attribute-workbook reference<input required className={inputClass} value={reference} onChange={event => setReference(event.target.value)} /></label></div><Button disabled={busy || !catalog || !manifest || !attributes}>{busy ? <LoaderCircle size={16} className="animate-spin" /> : <CheckCircle2 size={16} />}Validate batch</Button></form>
    <details className="text-sm"><summary>Configured document associations</summary>{catalog?.sources.length ? <ul className="mt-3 space-y-2">{catalog.sources.map(source => <li key={source.source_id} className="break-all">{source.reference} · {source.kind} · {source.product_ids.length} associated products{source.kind === "sharepoint" && " · Download blocked (401)"}</li>)}</ul> : <p className="mt-3 text-muted-foreground">No approved document associations configured.</p>}</details></section>
    <section className="border-t pt-5 space-y-4" aria-label="Batch queue"><div className="flex flex-wrap justify-between gap-3"><h2 className="text-lg font-semibold">Batch queue</h2><select aria-label="Saved batch" className={`${inputClass} max-w-lg`} value={batch?.id || ""} onChange={event => { const selected = history.find(value => value.id === event.target.value); if (selected) { setBatch(selected); setItems([]); setPage(0); setFilter("all"); } }}><option value="">Select a batch</option>{history.map(record => <option key={record.id} value={record.id}>{new Date(record.created_at).toLocaleString()} · {record.product_count} products · {record.state}</option>)}</select></div>
    {batch && <><div className="flex flex-wrap items-center gap-4 text-sm"><strong className="capitalize">{batch.state}</strong><span>{batch.product_count} products</span>{batch.progress && <span>{batch.progress.finished} finished · {batch.progress.unresolved} unresolved · {batch.progress.failed} failed</span>}<a className="inline-flex items-center gap-2 underline" href={`/api/batches/${batch.id}/export`}><Download size={16} />Export Excel</a></div>
    {!batch.mode && <div className="flex flex-wrap items-center gap-4"><select aria-label="Batch execution" className={`${inputClass} max-w-xs`} value={mode} onChange={event => { setMode(event.target.value); setConfirmed(false); }}><option value="evidence_only">Evidence validation only (no AI)</option><option value="live_inference">Live enrichment (approved scope only)</option></select>{mode === "live_inference" && <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />Authorize live execution for approved products</label>}<Button onClick={submit} disabled={busy || !batch.valid || (mode === "live_inference" && !confirmed)}><Play size={16} />Submit batch</Button></div>}
    <div className="flex flex-wrap justify-between items-center gap-3"><select aria-label="Item filter" className={`${inputClass} max-w-xs`} value={filter} onChange={event => { setFilter(event.target.value); setPage(0); }}><option value="all">All products</option><option value="failed">Failures / validation errors</option><option value="unresolved">Unresolved attributes</option><option value="pending">Pending review</option></select><div className="flex items-center gap-2 text-sm"><span>{total ? page * 50 + 1 : 0}-{Math.min((page + 1) * 50, total)} of {total}</span><Button size="icon" variant="outline" title="Previous page" aria-label="Previous page" disabled={page === 0} onClick={() => setPage(page - 1)}><ChevronLeft size={16} /></Button><Button size="icon" variant="outline" title="Next page" aria-label="Next page" disabled={(page + 1) * 50 >= total} onClick={() => setPage(page + 1)}><ChevronRight size={16} /></Button></div></div>
    <div className="overflow-x-auto"><table className="w-full text-sm text-left"><thead className="border-b"><tr>{["Item", "Vendor / MPN", "State", "Exceptions", "Review"].map(label => <th key={label} className="p-3 font-medium">{label}</th>)}</tr></thead><tbody>{items.map(item => <tr key={item.item_key} className="border-b align-top"><td className="p-3">{item.original["PIMITEM Number"]}</td><td className="p-3"><p>{item.original["Vendor Name"]}</p><p className="text-muted-foreground">{item.original.MPN}</p></td><td className="p-3 capitalize">{item.state}</td><td className="p-3 max-w-md break-words">{[...item.errors, item.error, ...item.warnings].filter(Boolean).join("; ") || "None"}</td><td className="p-3">{!["validated", "invalid", "queued"].includes(item.state) ? <Link className="underline" href={`/batches/${batch.id}/${item.item_key}`}>{item.pending_review ? "Pending review" : "Open evidence"}</Link> : "Not processed"}</td></tr>)}</tbody></table></div></>}
    </section><footer className="border-t pt-4 text-xs text-muted-foreground">{catalog?.identity.startsWith("development:") ? "Development identity is unverified. " : ""}No automatic approvals or master-data writes.</footer></div>;
}