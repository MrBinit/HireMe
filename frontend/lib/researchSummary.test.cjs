const test = require("node:test");
const assert = require("node:assert/strict");
const { evidenceRefHref, parseResearchSummary } = require("./researchSummary.js");

test('parseResearchSummary handles legacy text payload', () => {
  const parsed = parseResearchSummary('Legacy plain text summary.', null);
  assert.ok(parsed);
  assert.equal(parsed.parseError, true);
  assert.equal(parsed.legacySummary, 'Legacy plain text summary.');
  assert.deepEqual(parsed.issueFlags, []);
});

test('parseResearchSummary marks malformed JSON as parse error', () => {
  const parsed = parseResearchSummary('{"bad_json":', null);
  assert.ok(parsed);
  assert.equal(parsed.parseError, true);
  assert.equal(parsed.legacySummary, '{"bad_json":');
});

test('parseResearchSummary falls back to llm_analysis.issues when issue_flags is empty', () => {
  const payload = {
    issue_flags: [],
    llm_analysis: {
      confidence: 'medium',
      issues: [
        { type: 'skill_gap', severity: 'low', evidence: 'Missing Docker mention' },
        { type: 'experience_mismatch', severity: 'high', evidence: 'Timeline conflict' },
      ],
    },
  };
  const parsed = parseResearchSummary(JSON.stringify(payload), null);
  assert.ok(parsed);
  assert.equal(parsed.parseError, false);
  assert.equal(parsed.issueFlags.length, 2);
  assert.equal(parsed.issueFlags[0].type, 'experience_mismatch');
  assert.equal(parsed.issueFlags[0].source, 'llm_analysis');
  assert.equal(parsed.issueFlags[1].type, 'skill_gap');
});

test('parseResearchSummary sorts issue flags by severity high-medium-low-unknown', () => {
  const payload = {
    issue_flags: [
      { type: 'c', severity: 'low', source: 'linkedin', details: 'd3' },
      { type: 'd', severity: 'unknown', source: 'linkedin', details: 'd4' },
      { type: 'a', severity: 'high', source: 'linkedin', details: 'd1' },
      { type: 'b', severity: 'medium', source: 'linkedin', details: 'd2' },
    ],
  };
  const parsed = parseResearchSummary(JSON.stringify(payload), null);
  assert.ok(parsed);
  assert.deepEqual(
    parsed.issueFlags.map((item) => item.type),
    ['a', 'b', 'c', 'd'],
  );
});

test('parseResearchSummary extracts source links and evidenceRefHref maps anchors/urls', () => {
  const payload = {
    extractors: {
      linkedin: {
        matched_profile_url: 'https://linkedin.com/in/candidate',
        top_hits: [{ link: 'https://linkedin.com/posts/abc' }],
      },
      github: {
        profile_url: 'https://github.com/candidate',
        top_repositories: [{ name: 'hireme', html_url: 'https://github.com/candidate/hireme' }],
      },
      portfolio: {
        matched_portfolio_url: 'https://candidate.dev',
      },
    },
  };
  const parsed = parseResearchSummary(JSON.stringify(payload), null);
  assert.ok(parsed);
  assert.equal(parsed.sourceLinks.length, 5);
  assert.equal(evidenceRefHref('issue_flags[0].details'), '#research-issue-flags');
  assert.equal(evidenceRefHref('extractors.github.top_repositories[0]'), '#research-source-links');
  assert.equal(evidenceRefHref('https://example.com/evidence'), 'https://example.com/evidence');
});

test('parseResearchSummary tolerates null llm_analysis and deterministic_checks', () => {
  const payload = {
    issue_flags: [],
    llm_analysis: null,
    deterministic_checks: null,
  };
  assert.doesNotThrow(() => parseResearchSummary(JSON.stringify(payload), null));
  const parsed = parseResearchSummary(JSON.stringify(payload), null);
  assert.ok(parsed);
  assert.equal(parsed.confidence, null);
  assert.equal(parsed.manualReviewRequired, null);
});

test('parseResearchSummary derives confidence/manual review from issue severity when fields are missing', () => {
  const payload = {
    issue_flags: [
      { type: 'experience_mismatch', severity: 'high', source: 'linkedin', details: 'Timeline gap' },
      { type: 'skill_gap', severity: 'medium', source: 'github', details: 'Missing CI/CD' },
    ],
    llm_analysis: null,
    deterministic_checks: null,
  };
  const parsed = parseResearchSummary(JSON.stringify(payload), null);
  assert.ok(parsed);
  assert.equal(parsed.confidence, 'low');
  assert.equal(parsed.manualReviewRequired, true);
});
