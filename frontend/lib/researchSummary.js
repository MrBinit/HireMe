function normalizeTextList(value, maxItems) {
  if (!Array.isArray(value)) return [];
  const values = [];
  for (const item of value) {
    if (typeof item !== "string") continue;
    const text = item.trim();
    if (!text) continue;
    values.push(text);
    if (values.length >= Math.max(1, maxItems)) break;
  }
  return values;
}

function normalizeSeverity(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === "high") return "high";
  if (normalized === "medium") return "medium";
  if (normalized === "low") return "low";
  return "unknown";
}

function severityRank(value) {
  const normalized = normalizeSeverity(value);
  if (normalized === "high") return 0;
  if (normalized === "medium") return 1;
  if (normalized === "low") return 2;
  return 3;
}

function pushUniqueLink(links, seen, label, url) {
  if (typeof url !== "string") return;
  const normalizedUrl = url.trim();
  if (!/^https?:\/\//i.test(normalizedUrl)) return;
  if (seen.has(normalizedUrl)) return;
  seen.add(normalizedUrl);
  links.push({ label, url: normalizedUrl });
}

function extractSourceLinks(root) {
  const links = [];
  const seen = new Set();
  const extractors = root && typeof root.extractors === "object" ? root.extractors : {};

  const linkedin = extractors && typeof extractors.linkedin === "object" ? extractors.linkedin : {};
  pushUniqueLink(links, seen, "LinkedIn Profile", linkedin.matched_profile_url);
  if (Array.isArray(linkedin.top_hits)) {
    for (const hit of linkedin.top_hits.slice(0, 6)) {
      if (!hit || typeof hit !== "object") continue;
      pushUniqueLink(links, seen, "LinkedIn Hit", hit.link);
    }
  }

  const github = extractors && typeof extractors.github === "object" ? extractors.github : {};
  pushUniqueLink(links, seen, "GitHub Profile", github.profile_url);
  if (Array.isArray(github.top_repositories)) {
    for (const repo of github.top_repositories.slice(0, 8)) {
      if (!repo || typeof repo !== "object") continue;
      const name = typeof repo.name === "string" && repo.name.trim() ? repo.name.trim() : "Repo";
      pushUniqueLink(links, seen, `GitHub: ${name}`, repo.html_url);
    }
  }

  const portfolio =
    extractors && typeof extractors.portfolio === "object" ? extractors.portfolio : {};
  pushUniqueLink(links, seen, "Portfolio", portfolio.matched_portfolio_url);
  if (Array.isArray(portfolio.top_hits)) {
    for (const hit of portfolio.top_hits.slice(0, 6)) {
      if (!hit || typeof hit !== "object") continue;
      pushUniqueLink(links, seen, "Portfolio Hit", hit.link);
    }
  }

  return links;
}

function parseFromObject(root, options = {}) {
  const parseError = Boolean(options.parseError);
  const legacySummary = typeof options.legacySummary === "string" ? options.legacySummary : null;

  const issueFlagsRaw = root.issue_flags;
  const issueFlags = [];
  if (Array.isArray(issueFlagsRaw)) {
    for (const item of issueFlagsRaw.slice(0, 12)) {
      if (!item || typeof item !== "object") continue;
      const detailsValue = item.details;
      let details = "";
      if (typeof detailsValue === "string") {
        details = detailsValue.trim();
      } else if (detailsValue && typeof detailsValue === "object") {
        try {
          details = JSON.stringify(detailsValue);
        } catch {
          details = "";
        }
      }
      issueFlags.push({
        type: String(item.type || "").trim() || "unknown",
        severity: String(item.severity || "").trim() || "unknown",
        source: String(item.source || "").trim() || "unknown",
        details: details || "No details provided.",
      });
    }
  }

  const llmAnalysisValue = root ? root.llm_analysis : null;
  const llmAnalysis =
    llmAnalysisValue && typeof llmAnalysisValue === "object" ? llmAnalysisValue : {};
  const deterministicValue = root ? root.deterministic_checks : null;
  const deterministic =
    deterministicValue && typeof deterministicValue === "object" ? deterministicValue : {};

  const provenance = [];
  if (Array.isArray(llmAnalysis.provenance)) {
    for (const item of llmAnalysis.provenance.slice(0, 8)) {
      if (!item || typeof item !== "object") continue;
      const claim = String(item.claim || "").trim();
      const evidenceRefs = normalizeTextList(item.evidence_refs, 6);
      if (!claim || evidenceRefs.length === 0) continue;
      provenance.push({ claim, evidenceRefs });
    }
  }

  if (issueFlags.length === 0 && Array.isArray(llmAnalysis.issues)) {
    for (const item of llmAnalysis.issues.slice(0, 12)) {
      if (!item || typeof item !== "object") continue;
      issueFlags.push({
        type: String(item.type || "").trim() || "unknown",
        severity: String(item.severity || "").trim() || "unknown",
        source: "llm_analysis",
        details: String(item.evidence || "").trim() || "No evidence provided.",
      });
    }
  }

  issueFlags.sort((left, right) => {
    const rankDelta = severityRank(left.severity) - severityRank(right.severity);
    if (rankDelta !== 0) return rankDelta;
    return left.type.localeCompare(right.type);
  });

  const hasHighSeverityIssue = issueFlags.some((item) => normalizeSeverity(item.severity) === "high");
  const hasMediumSeverityIssue = issueFlags.some(
    (item) => normalizeSeverity(item.severity) === "medium",
  );

  const confidence =
    typeof llmAnalysis.confidence === "string" && llmAnalysis.confidence.trim()
      ? llmAnalysis.confidence.trim()
      : typeof deterministic.confidence_baseline === "string" &&
          deterministic.confidence_baseline.trim()
        ? deterministic.confidence_baseline.trim()
        : hasHighSeverityIssue
          ? "low"
          : hasMediumSeverityIssue
            ? "medium"
            : issueFlags.length > 0
              ? "high"
        : null;

  const manualReviewRequired =
    typeof deterministic.manual_review_required === "boolean"
      ? deterministic.manual_review_required
      : hasHighSeverityIssue
        ? true
        : issueFlags.length > 0
          ? false
      : null;

  return {
    issueFlags,
    discrepancies: normalizeTextList(root.discrepancies, 12),
    confidence,
    manualReviewRequired,
    deterministicNotes: normalizeTextList(deterministic.notes, 8),
    provenance,
    parseError,
    legacySummary,
    sourceLinks: extractSourceLinks(root),
  };
}

function parseResearchSummary(raw, typedSummary) {
  if (typedSummary && typeof typedSummary === "object") {
    return parseFromObject(typedSummary);
  }

  if (!raw || typeof raw !== "string" || !raw.trim()) return null;
  const trimmedRaw = raw.trim();
  try {
    const payload = JSON.parse(trimmedRaw);
    if (!payload || typeof payload !== "object") {
      return parseFromObject({}, { parseError: true, legacySummary: trimmedRaw });
    }
    return parseFromObject(payload);
  } catch {
    return parseFromObject({}, { parseError: true, legacySummary: trimmedRaw });
  }
}

function evidenceRefHref(ref) {
  const value = String(ref || "").trim();
  if (!value) return "#research-flags-evidence";
  if (/^https?:\/\//i.test(value)) return value;
  if (value.startsWith("issue_flags")) return "#research-issue-flags";
  if (value.startsWith("discrepancies")) return "#research-discrepancies";
  if (value.startsWith("cross_checks")) return "#research-discrepancies";
  if (value.startsWith("extractors")) return "#research-source-links";
  return "#research-flags-evidence";
}

module.exports = {
  evidenceRefHref,
  normalizeSeverity,
  parseResearchSummary,
};
