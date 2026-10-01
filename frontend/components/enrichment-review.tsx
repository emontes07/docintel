"use client";

import { useRef, useState } from "react";
import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { sourceLabel } from "@/utils/pilot-presentation";

type Candidate = { value: string | number | boolean; unit: string | null; evidence_ids: string[] };
type Decision = { decision: string; reviewer: string; reviewed_at: string; reason: string; corrected_value: string | number | boolean | null; corrected_unit: string | null };
type Attribute = { attribute_id: string; status: string; candidates: Candidate[]; review: Decision | null; review_annotations: { candidate_index: number; text: string; author: string; annotated_at: string }[] };
export type ReviewMachine = { attributes: Attribute[]; evidence: { evidence_id: string; text: string; source_locator: string; source_version: string }[]; manifest: { attributes: { attribute_id: string; value_type: string; unit: string | null }[]; existing_values: Record<string, string | number | boolean> } };
type Identity = "verified_entra" | "development_unverified" | "locally_entered_unverified";
const inputClass = "w-full min-w-0 rounded-md border bg-background px-3 py-2 text-sm";

function DecisionForm({ attribute, definition, identity, onReview }: { attribute: Attribute; definition: { value_type: string; unit: string | null }; identity: Identity; onReview: (value: Record<string, unknown>) => Promise<void> }) {
  const [decision, setDecision] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [reason, setReason] = useState("");
  const [correction, setCorrection] = useState("");
  const [candidate, setCandidate] = useState("0");
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [error, setError] = useState("");
  const manualIdentity = identity === "locally_entered_unverified";
  if (attribute.review) {
    const review = attribute.review;
    return <div className="border-l-2 border-emerald-600 pl-4 text-sm space-y-2" data-testid="recorded-review"><p className="font-semibold">{review.decision === "correct" ? "Corrected" : review.decision === "approve" ? "Approved" : "Rejected"}</p>{review.decision === "correct" && <p>Reviewed value: <strong>{String(review.corrected_value)} {review.corrected_unit}</strong></p>}<p className="break-words">{review.reason}</p><p className="text-muted-foreground break-all">{review.reviewer} · {identity === "verified_entra" ? "Verified Entra identity" : "Unverified development/local reviewer"} · {new Date(review.reviewed_at).toLocaleString()}</p></div>;
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true; setBusy(true); setError("");
    try {
      const value = ["number", "integer"].includes(definition.value_type) ? Number(correction) : definition.value_type === "boolean" ? correction === "true" : correction;
      if (decision === "correct" && (!correction.trim() || (typeof value === "number" && !Number.isFinite(value)))) throw new Error("Enter a valid correction");
      await onReview({ attribute_id: attribute.attribute_id, decision, reason: reason.trim(), ...(manualIdentity ? { reviewer: reviewer.trim() } : {}), candidate_index: decision === "approve" ? Number(candidate) : null, corrected_value: decision === "correct" ? value : null, corrected_unit: decision === "correct" ? definition.unit : null });
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Review failed"); }
    finally { submitting.current = false; setBusy(false); }
  }
  return <form onSubmit={submit} className="space-y-3"><div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">Decision<select aria-label={`${attribute.attribute_id} decision`} className={inputClass} required value={decision} onChange={event => setDecision(event.target.value)}><option value="">Choose a decision</option><option value="approve" disabled={!attribute.candidates.length}>Approve candidate</option><option value="correct">Correct</option><option value="reject">Reject</option></select></label>{manualIdentity && <label className="text-sm">Reviewer (locally entered, unverified)<input required className={inputClass} maxLength={120} value={reviewer} onChange={event => setReviewer(event.target.value)} /></label>}</div>
    {decision === "approve" && attribute.candidates.length > 1 && <label className="block text-sm">Candidate<select className={inputClass} value={candidate} onChange={event => setCandidate(event.target.value)}>{attribute.candidates.map((value, index) => <option key={index} value={index}>{String(value.value)} {value.unit}</option>)}</select></label>}
    {decision === "correct" && <label className="block text-sm">Corrected value {definition.unit && `(${definition.unit})`}{definition.value_type === "boolean" ? <select required className={inputClass} value={correction} onChange={event => setCorrection(event.target.value)}><option value="">Choose</option><option value="true">True</option><option value="false">False</option></select> : <input required className={inputClass} type={["number", "integer"].includes(definition.value_type) ? "number" : "text"} step={definition.value_type === "integer" ? "1" : "any"} value={correction} onChange={event => setCorrection(event.target.value)} />}</label>}
    <label className="block text-sm">Reason<textarea required className={inputClass} rows={2} maxLength={2000} value={reason} onChange={event => setReason(event.target.value)} /></label>{error && <p role="alert" className="text-sm text-red-600">{error}</p>}<Button disabled={busy || !decision || !reason.trim() || (manualIdentity && !reviewer.trim())}><Check size={16} />Record decision</Button></form>;
}

export function EnrichmentReview({ result, identity, onReview }: { result: ReviewMachine; identity: Identity; onReview?: (value: Record<string, unknown>) => Promise<void> }) {
  return <>{result.attributes.map(attribute => {
    const definition = result.manifest.attributes.find(value => value.attribute_id === attribute.attribute_id)!;
    return <section key={attribute.attribute_id} aria-label={attribute.attribute_id} className="border-t pt-5 space-y-4"><div className="flex flex-wrap justify-between gap-2"><h3 className="text-lg font-semibold">{attribute.attribute_id}</h3><span className="text-sm text-muted-foreground">{attribute.review ? "Reviewed" : "Pending review"} · {attribute.status.replaceAll("_", " ")}</span></div>
      {attribute.status === "existing" && <p>Existing value: {String(result.manifest.existing_values[attribute.attribute_id])}</p>}{!attribute.candidates.length && attribute.status !== "existing" && <p className="text-sm text-muted-foreground">No supported candidate proposed.</p>}
      {attribute.candidates.map((candidate, index) => <div key={index} className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]"><div><p className="text-xs uppercase text-muted-foreground">Original model proposal</p><p className="mt-1 text-xl font-semibold break-words">{String(candidate.value)} {candidate.unit}</p></div><div className="min-w-0 space-y-3">{candidate.evidence_ids.map(id => {
        const evidence = result.evidence.find(value => value.evidence_id === id);
        if (!evidence) return <p role="alert" key={id}>Citation unavailable</p>;
        const location = sourceLabel(evidence.source_locator);
        return <div key={id} className="border-l-2 border-emerald-600 pl-3"><p className="text-xs font-medium text-emerald-700 dark:text-emerald-400">Source excerpt</p><p className="mt-2 text-sm font-medium break-all">{location.filename}</p><p className="text-xs text-muted-foreground">{location.position}</p><blockquote className="my-2 text-sm break-words">{evidence.text}</blockquote><details className="text-xs text-muted-foreground"><summary>Full source locator</summary><p className="break-all">{evidence.source_locator}</p></details></div>;
      })}{attribute.review_annotations.filter(note => note.candidate_index === index).map((note, noteIndex) => <aside key={noteIndex} data-testid="qualification" className="border-l-2 border-amber-500 pl-3 text-sm"><p className="font-medium text-amber-800 dark:text-amber-400">Post-generation qualification · Not source evidence</p><p className="mt-1 break-words">{note.text}</p><p className="mt-1 text-xs text-muted-foreground break-words">{note.author} · {new Date(note.annotated_at).toLocaleString()}</p></aside>)}</div></div>)}
      {attribute.status !== "existing" && onReview && <div className="border-t border-dashed pt-4 space-y-3"><h4 className="text-sm font-semibold">Human decision · {identity === "verified_entra" ? "Verified identity" : "Unverified reviewer"}</h4><DecisionForm attribute={attribute} definition={definition} identity={identity} onReview={onReview} /></div>}
    </section>;
  })}</>;
}