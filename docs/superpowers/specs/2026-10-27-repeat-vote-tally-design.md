# Repeat-Vote Tally Design

## Goal

Count the latest applicable repeat-vote RG-2 for each polling station without mixing multiple voting rounds into the unofficial total. Prevent a document uploaded under the wrong RIK type from displacing a legitimate result, while preserving every document and operator decision for audit.

## Round Selection

Treat RG-2 document types as successive station-result rounds with this precedence:

1. `16` — RG-2 repeat vote III
2. `15` — RG-2 repeat vote II
3. `10` — RG-2 repeat vote I
4. `6` — original RG-2

For each station, select exactly one active, legitimate RG-2. Type precedence wins; upload time and document ID only break ties within the same type. All lower-precedence forms remain visible but are marked as superseded and contribute nothing to either corrected or as-filed totals.

Do not fall back to an earlier round when the selected legitimate form is unanalysed, held, unreadable, incomplete, or otherwise excluded. The station remains pending or excluded until the selected form can contribute. A removed form is not a candidate.

The as-filed view means the selected round's numbers before RG-7 or operator corrections. It does not mean the station's original type-6 numbers.

## Decisions and Corrections

Keep first-round and repeat-vote decision families separate:

- A selected type-6 RG-2 receives RG-7 types 19–22 and is excluded by active RG-8 type 8 or RG-9 type 9.
- A selected repeat RG-2 (type 10, 15, or 16) can receive repeat-vote RG-7 types 23–26 and can be excluded by active RG-8-R type 27 or RG-9-R type 28, but only when the decision is bound to that repeat round as described below.

RG-7 remains a field-level overlay on the selected form. A repeat RG-2 is instead a complete replacement for the prior round. Existing RG-7 validation, ordering, arithmetic re-evaluation, and corrected-versus-filed behavior remain unchanged within the selected decision family.

RIK's repeat-decision types do not identify repeat I, II, or III. Bind them conservatively by chronology:

- The start of a repeat round is the earliest parseable RIK upload time among legitimate documents of that RG-2 type. Replacements within the same type do not start another round.
- A repeat decision belongs to the latest repeat round whose start is strictly earlier than the decision's RIK upload time.
- Apply a repeat decision only when its bound round type is the selected RG-2's type. An active decision bound to an earlier round remains visible but cannot correct or exclude a later repeat.
- A decision with a missing or unparseable upload time, no preceding legitimate repeat round, or a time equal to a round start is ambiguous. Do not apply it automatically; show an operator warning and retain its RIK link.
- A quarantined or uncertain RIK-listed RG-2 does not establish a round start. Confirming it as RG-2 can establish the start and causes bindings to be recomputed.
- RG-7's old-value consistency check remains an additional safeguard after chronological binding.

This binding uses stored RIK upload times, not local detection times or parsed dates printed inside a decision. It is deliberately conservative: when applicability cannot be established, totals remain based on the selected RG-2 without that decision rather than risking a correction or exclusion from another vote.

Operator numeric verification and correction must support the currently selected legitimate RG-2 of any of the four types. Corrections remain attached to an exact document URL and require a reason. They do not carry forward when a later repeat form supersedes that URL. Holds also remain URL-specific, so a hold on a superseded form does not hold its replacement.

## Mislabeled-Document Quarantine

Extend RG-2 analysis to classify the scanned content as `rg2`, `rg7`, `other`, or `uncertain`, with concise evidence. This is a content classification independent of the type assigned by RIK.

When a RIK-listed RG-2 is confidently classified as RG-7:

- quarantine it as a likely mislabeled RG-7;
- exclude it from round precedence and all totals;
- show the warning, evidence, and RIK link in the Forms table;
- continue selection using the highest legitimate RG-2 beneath it; and
- never reinterpret, parse, or apply it automatically as an RG-7.

An `uncertain` document also cannot supersede an established legitimate RG-2 and remains visibly pending operator review. If no legitimate form exists, the station remains pending rather than contributing guessed or stale numbers.

A document classified as `other` follows the same safe eligibility rule as a document classified as RG-7: quarantine it, exclude it from precedence and totals, allow fallback to a lower legitimate form, and show **Quarantined: not an RG-2** with its evidence and RIK link.

Provide an operator action on an RG-7, `other`, or `uncertain` document to confirm it as an RG-2. The override is stored against the exact URL and permits normal round precedence. It must not alter RIK metadata or silently reclassify other document versions.

To use a quarantined document's correction manually, the operator opens numeric verification for the selected legitimate RG-2, enters a complete corrected snapshot, and records a reason referencing the mislabeled document's RIK link. The quarantined document itself remains unchanged and unapplied.

Adding content classification changes the analysis schema and prompt version. Existing reports have no inferred classification and require reanalysis before classification-dependent behavior is available. Reanalysis can incur model API cost; ordinary analysis should not require an additional model request.

## Operator and Public Presentation

Forms rows distinguish these states:

- counted round;
- superseded by repeat vote;
- quarantined as a likely mislabeled RG-7;
- quarantined as not an RG-2; and
- pending document classification.

Repeat decisions also distinguish **applied to selected repeat**, **belongs to superseded repeat**, and **round ambiguous**. Non-applied decisions retain their RIK links and do not silently disappear.

Operator results identify whether the selected source is the original RG-2 or repeat I, II, or III. The selected form's existing reconciliation, inclusion, hold, review, RG-7, and RIK-link details continue to work.

Public results use the resulting selected-round totals and may identify the source round where station-level results are already shown, but they gain no private document URLs, findings, notes, model details, or audit history. Existing export privacy boundaries remain unchanged.

## Failure and Safety Behavior

- A higher-precedence legitimate form blocks fallback even when it cannot currently contribute.
- A quarantined or uncertain upload cannot suppress a lower-precedence legitimate form.
- First-round decisions never affect a selected repeat form, repeat decisions never affect a selected original form, and repeat decisions bound to an earlier repeat never affect a later one.
- A classification override affects only its exact document URL.
- Removing or replacing a document recomputes selection from the remaining active candidates.
- Missing or malformed classification data fails to the pending-review state rather than being treated as a confirmed RG-2.
- Missing, equal, or malformed decision/round upload times fail to an unapplied ambiguous-decision state.

## Verification

Add focused regression coverage for:

- precedence across types 6, 10, 15, and 16;
- tie-breaking and active/removed versions;
- no fallback from a legitimate but unanalysed, held, or incomplete repeat form;
- quarantine fallback to the highest legitimate form;
- `other` and uncertain documents, higher-precedence quarantine fallback, and exact-URL classification overrides;
- separation and application of first-round and repeat RG-7/RG-8/RG-9 families;
- repeat decisions before and after repeat-II and repeat-III round starts, including older decisions that remain active;
- ambiguous missing, equal, and malformed decision/round timestamps;
- same-round RG-2 replacements that do not reset the round start or lose applicable decisions;
- corrected and as-filed totals from the selected round;
- operator corrections on selected repeat forms and their non-transfer to replacements;
- URL-specific holds on selected and superseded forms;
- Forms-table and operator-result status/source labels; and
- aggregate and public-result outputs without privacy regressions.

Run the focused tally, RG-7, operator-correction, browse, and public-results tests, then the full test suite. Inspect the final diff for unrelated edits.
