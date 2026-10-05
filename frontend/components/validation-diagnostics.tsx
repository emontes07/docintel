type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export type ValidationDiagnostic = {
  schema_version: 1;
  stage: "structured_response_parsing" | "evidence_validation";
  issues: { field_path: string; message: string }[];
  used_references: { field_path: string; value: string | null; sha256: string; redacted: boolean }[];
  valid_references: string[];
  parsed_response: { [key: string]: JsonValue };
  raw_response_sha256: string | null;
  raw_response_hash_basis: "provider_content" | "unavailable";
  source_tier: string | null;
  reservation_id: string | null;
  prompt_format: string | null;
  truncated: boolean;
};

export function ValidationDiagnostics({ diagnostics }: { diagnostics?: ValidationDiagnostic[] }) {
  return <section aria-label="Validation diagnostics" className="border-t pt-5 space-y-4">
    <h2 className="text-lg font-semibold">Validation diagnostics</h2>
    {!diagnostics?.length ? <p className="text-sm text-muted-foreground">No validation diagnostics recorded. This does not establish successful validation.</p> : diagnostics.map((diagnostic, index) => <article key={index} className="space-y-3 border-l-2 border-amber-500 pl-3 text-sm">
      <h3 className="font-semibold">{diagnostic.stage === "structured_response_parsing" ? "Structured response parsing" : "Evidence validation"}</h3>
      {diagnostic.truncated && <p role="note" className="font-medium">Diagnostic output was truncated; some recorded details may be omitted.</p>}
      <div className="space-y-1">
        <h4 className="font-medium">Issues and field paths</h4>
        {diagnostic.issues.length ? <ul className="space-y-2">{diagnostic.issues.map((issue, issueIndex) => <li key={issueIndex}><code className="break-all">{issue.field_path}</code><p className="whitespace-pre-wrap break-words">{issue.message}</p></li>)}</ul> : <p>No issues recorded in this diagnostic.</p>}
      </div>
      <div className="space-y-1">
        <h4 className="font-medium">Supplied references</h4>
        {diagnostic.used_references.length ? <ul className="space-y-2">{diagnostic.used_references.map((reference, referenceIndex) => <li key={referenceIndex} className="space-y-1">
          <p>Field path: <code className="break-all">{reference.field_path}</code></p>
          <p className="whitespace-pre-wrap break-all">Value: {reference.redacted ? "Redacted" : reference.value ?? "Unavailable"}</p>
          <p>Redaction: {reference.redacted ? "Redacted" : "Not redacted"}</p>
          <p className="break-all">Reference SHA-256: <code>{reference.sha256}</code></p>
        </li>)}</ul> : <p>No supplied references recorded.</p>}
      </div>
      <div className="space-y-1">
        <h4 className="font-medium">Valid references</h4>
        {diagnostic.valid_references.length ? <ul className="space-y-1">{diagnostic.valid_references.map((reference, referenceIndex) => <li key={referenceIndex}><code className="break-all">{reference}</code></li>)}</ul> : <p>No valid references recorded.</p>}
      </div>
      <div className="space-y-1">
        <h4 className="font-medium">Parsed response (sanitized/redacted)</h4>
        <pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify(diagnostic.parsed_response, null, 2)}</pre>
      </div>
      <dl className="space-y-1 break-all">
        <div><dt className="font-medium">Raw response SHA-256</dt><dd>{diagnostic.raw_response_hash_basis === "provider_content" && diagnostic.raw_response_sha256 ? diagnostic.raw_response_sha256 : "Unavailable — no raw provider-content hash recorded."}</dd></div>
        <div><dt className="font-medium">Raw response hash basis</dt><dd>{diagnostic.raw_response_hash_basis === "provider_content" ? "Provider content" : "Unavailable"}</dd></div>
        <div><dt className="font-medium">Source tier</dt><dd>{diagnostic.source_tier ?? "Not recorded"}</dd></div>
        <div><dt className="font-medium">Reservation ID</dt><dd>{diagnostic.reservation_id ?? "Not recorded"}</dd></div>
        <div><dt className="font-medium">Prompt format</dt><dd>{diagnostic.prompt_format ?? "Not recorded"}</dd></div>
        <div><dt className="font-medium">Diagnostic schema version</dt><dd>{diagnostic.schema_version}</dd></div>
      </dl>
    </article>)}
  </section>;
}
