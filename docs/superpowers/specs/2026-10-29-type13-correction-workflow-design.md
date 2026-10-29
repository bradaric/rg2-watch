# Type-13 Correction Workflow Design

## Purpose

RIK publishes some substantive corrections to polling-station minutes under document type 13,
`Исправка решења`, rather than an RG-7 document type. Type 13 is heterogeneous: a document may
correct RG-2 numbers, an RG-7 or RG-8 decision, or another decision. The watcher must detect and
surface every type-13 document without treating every one as an RG-7.

The workflow will attach each type-13 document to its polling station, use model analysis to
classify and extract proposed changes, and require an operator to approve any effect on unofficial
totals. It must also support complete manual handling when analysis is unavailable or inconclusive.

## Goals

- Detect type-13 documents through the fast rare-document lane and issue a D1 alert.
- Download and cache the source PDF through the existing guarded fetch path.
- Classify whether the document corrects RG-2 numbers or another decision.
- Extract old/new values when the source supports them.
- Attach the source document to the correct RG-2 row when a target can be identified.
- Reuse the existing operator-correction dialog and validation path.
- Require operator confirmation before changing totals or resolving numeric findings.
- Let the operator classify and enter values manually when model analysis is insufficient.
- Preserve source, correction, finding, and review history for audit.

## Non-goals

- Automatically applying a type-13 document based only on model confidence.
- Treating every type-13 document as an RG-7.
- Hiding type-13 documents that do not correct RG-2 totals.
- Deleting original findings or human review history after a correction.
- Building a second correction editor.

## Detection and analysis

Document type 13 will join the rare-document watch set and status reporting. An added type-13
document produces a MEDIUM D1 finding and is queued for analysis when analysis is enabled. The
normal baseline rules continue to suppress initial alerts when requested.

Type 13 uses a dedicated analysis schema and prompt. Its normalized result contains:

- a target classification: `rg2`, `other_decision`, or `uncertain`;
- concise visible evidence;
- confidence;
- decision date and station number when readable;
- RG-2-style old/new changes and corrected-table values when applicable; and
- a short description of the corrected decision when it is not an RG-2 correction.

Analysis is advisory. No model result changes the tally or resolves a finding.

## Target attachment

The election and station key from the RIK listing provide the primary attachment. A first-round
type-13 correction attaches to that station's selected first-round RG-2. For repeat voting, the
existing round-binding rules use document chronology to identify the applicable RG-2 round.

If there is no eligible local RG-2 or the repeat round is ambiguous, the type-13 document remains
visible through the existing station-level attachment presentation, but numeric approval is
unavailable until the local target becomes unambiguous. This does not mean the RIK station is
unknown and does not introduce a separate assignment UI or workflow. The model cannot choose an
ambiguous target.

Documents classified as corrections to RG-7, RG-8, or another decision remain attached to the
station with no tally effect.

## Operator workflow

The RG-2 row displays attached type-13 documents with their source-PDF link, upload date, analysis
state, classification, evidence, confidence, and resolution state.

Available actions are:

- **Review correction**: open the existing operator-correction modal;
- **Enter manually**: open the same modal from the current effective values when extraction cannot
  provide a usable proposal;
- **Mark as other correction**: retain the attachment with no tally effect;
- **Mark uncertain**: retain it as unresolved follow-up work; and
- **Revoke**: reverse an earlier operator resolution.

For a usable RG-2 extraction, the existing modal is prefilled by applying the proposed changes to
the current effective RG-2 snapshot. It continues to show current and operator values for every
numeric field and list row, require a reason, validate a complete snapshot, and display revision
history. The modal additionally identifies and links the type-13 source document.

An operator may edit every prefilled value. Approval is disabled until the complete snapshot passes
the existing non-negative and reconciliation checks. Extracted old values that disagree with the
effective RG-2 are highlighted; they are never silently ignored.

## Persistence and provenance

Resolution history links:

- the type-13 source document and exact URL version;
- the source analysis revision, when analysis supplied the proposal;
- the exact target RG-2 document version;
- the resulting operator-correction revision, when approved;
- the operator action and reason; and
- creation time and revocation or suspension state.

The approved numeric snapshot remains an ordinary operator correction so tallying, arithmetic
validation, cache invalidation, revision conflict checks, and revocation share the existing path.
The type-13 resolution adds provenance and classification; it does not create a parallel correction
engine.

The latest active operator correction is one complete snapshot, with the existing replacement and
revocation semantics. Revoking it does not selectively undo values contributed by one source or
reactivate an older snapshot. If a later or overlapping correction must be incorporated, the
operator reviews the effective source and saves a new complete snapshot. The workflow does not
track field-level dependencies or automatically replay a chain of correction documents.

Writes that approve a type-13 resolution and its operator-correction snapshot must be atomic. A
failure cannot leave a correction applied without its source resolution, or mark a source resolved
without applying the approved snapshot.

## Findings, reconciliation, and reviews

After approval, the corrected snapshot is evaluated using the existing tally and rule machinery.
Resolution is outcome-based:

- arithmetic or numeric findings fixed by the approved values become inactive and display
  `resolved by type-13 correction` with a source link;
- findings that still evaluate against the corrected snapshot remain active;
- unrelated findings remain active; and
- invalid corrected values cannot be saved merely to clear an alert.

Original findings remain stored and visible in history. They are not deleted or rewritten.

Type-13 approval does not change a human review verdict. If the target RG-2 has a current
`escalate` verdict, it remains held after the numeric correction even when the corrected values
reconcile. The UI states that the correction was applied but the document remains escalated. After
reviewing the corrected result, the operator separately records the appropriate review verdict
using the existing controls; review history remains unchanged by the correction workflow.

## Staleness and failure handling

- Download failures use existing retry and permanent-failure behavior. The attachment and D1
  finding remain visible.
- Model failures show pending or failed analysis and leave manual actions available.
- Low-confidence or incomplete extraction is presented as a proposal requiring review, never as an
  approved change.
- A source-token check covers the target RG-2 analysis, applicable earlier RG-7 decisions, the
  type-13 URL version, and the type-13 analysis revision.
- If any covered source changes while the modal is open, save fails with a conflict and requires a
  refreshed review.
- Replacement or removal of an approved type-13 source suspends its resolution and correction until
  an operator reviews the new state.
- Ambiguous repeat-round binding blocks numeric approval until the local target is unambiguous.
- `other_decision` and `uncertain` states never affect totals.
- Revocation removes the active complete operator snapshot and returns numeric evaluation to the
  effective RG-2 plus applicable RG-7 sources. It does not alter the separate review verdict.

## UI behavior

The type-13 attachment is rendered inside the existing RG-2 row details alongside RG-7 and other
follow-up information. It does not create a separate result row or a new popup. Status styling must
use the application's adaptive color tokens and preserve contrast in both light and dark themes.

The existing correction modal gains an optional source-document section and optional prefilled
proposal. Ordinary operator corrections continue to open and behave exactly as before.

## Testing

Focused automated coverage will verify:

- type 13 participates in rare count checks, sweeps, D1 findings, status, and analysis queuing;
- normalization distinguishes RG-2, other-decision, and uncertain documents;
- first-round attachment and ambiguous repeat-round behavior;
- model extraction alone never changes totals;
- a BM 32-style proposal prefills the existing correction modal correctly;
- manual classification and complete manual value entry;
- approved values pass through existing full-snapshot validation;
- approval updates corrected totals and resolves only findings no longer produced;
- unrelated or still-failing findings remain active;
- approving a correction does not change an escalated review, and the station remains held until
  the operator records a separate review verdict;
- other-decision and uncertain resolutions remain attached without tally effects;
- stale target analysis, changed type-13 analysis, source replacement/removal, conflicting saves,
  revocation, and invalid arithmetic are safe;
- approval and provenance writes are atomic; and
- existing RG-7 and ordinary operator-correction tests remain unchanged and passing.

## Success criteria

For a document such as Savski Venac BM 32, the watcher alerts and downloads the type-13 PDF, the
model proposes the visible changes, and the source appears under BM 32's RG-2. Nothing changes until
the operator reviews the existing correction form. After approval, the validated corrected totals
are available and fixed numeric errors show as resolved with full history. If the RG-2 remains
manually escalated, it stays held until the operator separately updates the review verdict;
unrelated alerts remain active.
