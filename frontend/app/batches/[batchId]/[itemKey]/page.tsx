"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { EnrichmentReview, ReviewMachine } from "@/components/enrichment-review";

type Detail = { state: string; requested_mode: string; original: Record<string, string>; row: number; error?: string; provenance?: { reference: string; retrieval: string; parsing: string; error?: string }[]; machine_sha256?: string; reviewed_result: ReviewMachine | null; reviewer_identity: "verified_entra" | "development_unverified" };

export default function BatchItemPage({ params }: { params: Promise<{ batchId: string; itemKey: string }> }) {
  const { batchId, itemKey } = use(params);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const path = `/api/batches/${batchId}/items/${itemKey}`;
  useEffect(() => {
    let cancelled = false;
    fetch(path, { cache: "no-store" }).then(async response => { const value = await response.json(); if (!response.ok) throw new Error(value.detail); if (!cancelled) setDetail(value); }).catch(failure => { if (!cancelled) setError(failure.message); });
    return () => { cancelled = true; };
  }, [path]);
  async function review(value: Record<string, unknown>) {
    const response = await fetch(`${path}/reviews`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value), cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Review failed");
    setDetail(result);
  }
  return <div className="mx-auto max-w-6xl space-y-5 p-4 md:p-8"><Link className="text-sm underline" href="/batches">Back to batches</Link><h1 className="text-2xl font-semibold">Product evidence and review</h1>{error && <p role="alert" className="text-red-600">{error}</p>}{!detail && !error && <p role="status">Loading recorded result.</p>}{detail && <><p className="font-medium break-words">{detail.original["PIMITEM Number"]} · {detail.original["Vendor Name"]} · {detail.original.MPN}</p><p className="text-sm">Recorded execution: {detail.requested_mode} · {detail.state}</p><details className="text-sm"><summary>Original workbook row {detail.row}</summary><dl className="mt-3 space-y-2">{Object.entries(detail.original).map(([name, value]) => <div key={name}><dt className="font-medium">{name}</dt><dd className="break-all">{value || "(blank)"}</dd></div>)}</dl></details>{detail.error && <p role="alert" className="border-l-2 border-amber-500 pl-3 text-sm break-words">{detail.error}</p>}<ul className="text-sm space-y-2">{detail.provenance?.map((source, index) => <li key={index} className="break-all">{source.reference} · Retrieval: {source.retrieval} · Parsing: {source.parsing}{source.error && ` · ${source.error}`}</li>)}</ul><details className="text-xs"><summary>Immutable machine-result hash</summary><p className="break-all">{detail.machine_sha256 || "No machine result"}</p></details>{detail.reviewed_result && <EnrichmentReview key={`${batchId}/${itemKey}`} result={detail.reviewed_result} identity={detail.reviewer_identity} onReview={review} />}</>}</div>;
}