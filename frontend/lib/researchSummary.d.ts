export interface ParsedIssueFlag {
  type: string;
  severity: string;
  source: string;
  details: string;
}

export interface ParsedProvenance {
  claim: string;
  evidenceRefs: string[];
}

export interface ParsedSourceLink {
  label: string;
  url: string;
}

export interface ParsedResearchSummary {
  issueFlags: ParsedIssueFlag[];
  discrepancies: string[];
  confidence: string | null;
  manualReviewRequired: boolean | null;
  deterministicNotes: string[];
  provenance: ParsedProvenance[];
  parseError: boolean;
  legacySummary: string | null;
  sourceLinks: ParsedSourceLink[];
}

export function normalizeSeverity(value: unknown): "high" | "medium" | "low" | "unknown";
export function parseResearchSummary(
  raw: string | null | undefined,
  typedSummary?: unknown,
): ParsedResearchSummary | null;
export function evidenceRefHref(ref: unknown): string;
