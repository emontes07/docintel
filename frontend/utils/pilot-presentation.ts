type Parsing = { status: string; origin: string } | null;

export function executionLabel(mode: string): string {
  if (mode === "offline_replay") return "Replay";
  if (mode === "live_inference") return "Live inference";
  return "Unknown execution method";
}

export function stagePresentation(name: string, status: string, parsing: Parsing) {
  let outcome = status.replaceAll("_", " ");
  let method: string | null = null;
  if (name === "parsing") {
    method = parsing?.status === "live" ? "Fresh analysis" : parsing?.status === "cached" ? "Cache" : "Unknown method";
    if (["live", "cached"].includes(status)) outcome = parsing?.status === status ? "succeeded" : "unknown";
  } else if (name === "inference" && status === "replayed") {
    outcome = "succeeded";
    method = "Replayed output; no new inference";
  }
  const known = ["succeeded", "passed", "failed", "running", "pending", "queued", "not attempted", "unknown"];
  if (!known.includes(outcome)) outcome = `Unknown outcome (${outcome || "not recorded"})`;
  return { outcome, method, successful: outcome === "succeeded" || outcome === "passed" };
}

export function sourceLabel(locator: string) {
  const [location, fragment = ""] = locator.split("#", 2);
  let pathname = location;
  try { pathname = new URL(location).pathname; } catch { pathname = location; }
  const filename = pathname.split(/[\\/]/).filter(Boolean).at(-1) || "Source not recorded";
  const labels: Record<string, string> = { page: "Page", paragraph: "Paragraph", table: "Table", row: "Row", column: "Column", cell: "Cell" };
  const position = Array.from(new URLSearchParams(fragment), ([key, value]) => `${labels[key] || key}: ${value}`).join(" · ");
  try {
    return { filename: decodeURIComponent(filename), position: position || "Location not recorded" };
  } catch {
    return { filename, position: position || "Location not recorded" };
  }
}