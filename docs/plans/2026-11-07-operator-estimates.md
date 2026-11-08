# Operator-only national estimates

## Agreed scope and priorities

Playground Rules authorized implementation in the current project. Preserve the existing counted
tally and public export. No paid reanalysis, network fetching, publication, live-service restarts,
Git commits, or changes to existing election data are needed.

1. Add a reproducible, frozen municipality-stratified random station sample and an all-arrivals
   projection. Both expand **vote totals**, not station percentages, using N_h / reporting_h.
2. Use the existing corrected station tally, including holds, decisions and repeat-vote selection.
   Reject impossible numeric readings. Never replace missing sampled stations with early reporters.
3. Add two private operator tabs with coverage, missing groups, conditional intervals, downloadable
   sample membership/status, and explicit experimental warnings. Municipal/council elections are
   unsupported. National scope supports both parliamentary and presidential ballots through the
   existing explicit tally_scope override when RIK's election type is ambiguous.
4. Provide offline sample/replay CLI commands. Replay uses current stored readings ordered by the
   selected form's upload timestamp: this is an arrival-bias experiment, NOT an as-of/live replay.
   Current corrections, reviews, repeat selection and analysis availability are final-state inputs.
5. Backtest several sample sizes/seeds against a near-complete stored reference; refuse accuracy
   claims when the reference is incomplete. Compare raw arrivals, fixed sample and all-arrivals.

## Statistical design and limitations

The sampling frame is the election-specific stored station roster, never the received-form pool.
Strata are municipalities, including foreign and institutional groups as stored. Require a nonempty
roster for every stored municipality, unique identities, and no observed RG-2 identity outside it.
Use a seeded deterministic random ranking of identities within each stratum. Allocate at least two
stations per non-singleton stratum (singletons are certainty strata); distribute remaining slots
proportionally to remaining capacity. Refuse undersized requests rather than drop municipalities.
Fingerprint the complete frame and validate a saved manifest against regenerated membership; roster
changes invalidate the sample instead of silently resampling. Creation after results exist requires
an explicit retrospective flag; the UI retains that label.

For each list/candidate estimate the ratio of expanded votes to expanded valid+invalid ballots,
matching the counted results denominator. Compute the usual stratified station-cluster ratio
linearization variance with a finite-population correction. This is a pointwise normal-approximation
95% interval under exchangeable reporting within each municipality, NOT a guarantee or an interval
covering systematic arrival bias, extraction errors or all candidates simultaneously. Fully returned
random samples have a probability-sampling interpretation; partial samples and all-arrivals require
the untestable exchangeable-reporting assumption. Zero observed variance in a non-census stratum is
not proof of certainty: suppress intervals/estimates when variance cannot be estimated usefully.

Fail closed for missing/sparse strata and malformed/stale manifests. Sample estimates additionally
require at least 80% selected stations reporting overall. These are diagnostic guardrails, not
historically validated accuracy thresholds. Show station coverage, not invented electorate coverage.
No winner calls, seats, presidential runoff eligibility or threshold certification.

## Implementation batches and checks

- Core `estimates.py`: pure sampling/estimation, Store frame adapter, manifest validation; tests
  first in `tests/test_estimates.py`, analytic ratios/variance, identity and coverage edge cases.
- Offline `estimate_replay.py` and CLI: read-only Store access, no-overwrite local outputs,
  timestamp ties, incomplete-reference safeguards, deterministic multiple-seed sample experiments.
- Private `browse.py` integration and `static/index.html` panels: share one complete snapshot,
  preserve feed revision handling, DOM text rendering, CSV escaping; public export regression tests.
- Run focused tests, synthetic experiments (explicitly synthetic), real-data replay, complete local
  test suite, JavaScript checks and final diff review. Record observed results and remaining gaps.

Implementation stays in this session without subagents or a new branch. Leave changes uncommitted.

## Observed implementation/verification status

- Implemented and tested both private panels, frozen manifests, coverage/identity guards,
  read-only sample/replay CLI commands and public-export isolation. No additional dependencies.
- Final-readings replay ran with 50 seeds per size. Provisional default is now 1,600 rather than
  800 based on the observed diagnostics; this is not a future precision guarantee.
- A labelled 1,600-station retrospective pilot manifest was created locally, without changing
  the election database. 1,599 selected stations are usable in the saved reference snapshot.
- Complete suite run: 1,215 passed, 1 live-API test skipped. Later focused checks cover the final
  frame, clock, presidential-scope and denominator edge cases. JavaScript syntax and diff checks pass.
- Scientific validation remains incomplete: reference has seven missing/unusable stations,
  nominal interval coverage was below 95%, release gates were late, and independent historical
  presidential readings are sparse. No early-election accuracy or winner-call claim is made.

## Corrected requirement: historical design before the upcoming election

The user clarified that the desired fixed sample must be designed from the **previous election's
station results AND reporting times**, not built from upcoming returns. The geography-only random
pilot does not meet that requirement. Preserve it as a labelled retrospective comparison, not as
the recommended upcoming-election design.

Use the most complete completed national dataset available (779676) as a historical design source.
Compare smaller fixed sizes (200/300/400/600/800) and predeclared early-reporting preferences across multiple
random seeds. Use ordered pivotal
sampling with known, strictly positive inclusion probabilities, historical political/size ordering,
and early-upload preference. Calculate actual inverse-probability design weights, not ad-hoc weights
inferred from the upcoming arrival pool. Keep all source stations eligible, including uncertain profiles.

The municipal and hard-grid experiments were measured and found to keep release closed for days.
Default to a single fixed national probability draw, using geographic cells as the first ordering
axis and prior political profiles/size as subsequent axes (implicit stratification). Retain positive
inclusion probability for every station; do not require a form from every tiny special domain.
Check design-weighted prior geographic/electorate balance explicitly rather than confusing a
municipality/foreign-domain quota with representativeness. Hard grid/municipal strata remain comparisons.
Require both 80% count response and 80% selected design-weighted prior-electorate response. The latter
is a historical proxy, not actual current electorate coverage. No early release is manufactured by
silently discarding missing high-weight selected stations.

Freeze a prior-profile readiness policy too: <=2pp prior political-share residual, <=3pp prior
geographic-electorate-share residual, <=5% unknown prior-profile mass. This withholds an unbalanced
partial sample without reselecting it. Replay release errors under this gate are circular/in-sample
balance diagnostics, NOT independent accuracy evidence. Retain ungated full-sample diagnostics for
all trials and state the independent-validation gap prominently.

Historical diagnostics must compare weighted prior vote shares/electorate coverage and first-upload
availability, including release rate, time to 80%/90% and geographic release requirements. Do not select
a lucky seed or claim independent validation on the same election used for design. Final prior readings
and first-original-upload times are retrospective predictors, not proof of as-of numeric availability.

Produce a historical design proposal now. Activate it only against an explicitly selected upcoming
election roster after station mapping/boundaries have been confirmed; refuse unmatched rosters instead
of inventing mappings. No upcoming votes or reporting order may influence selection. Live results use
the frozen inclusion weights with within-sampling-group response adjustment. The existing SRS variance
formula is not valid for an unequal-probability pivotal design: show no forecast confidence interval
until a suitable variance/calibration method is validated. Keep historical errors separate.

Implement and verify this correction directly under the existing Playground authorization. Do not
change current live services, overwrite the existing pilot manifest, fetch/pay for missing analyses,
publish, or commit.

## Corrected implementation verification (8 November 2026)

- Added historical-only design CLI, frozen pivotal manifests/inclusion weights, explicit upcoming
  roster confirmation/activation, historical-profile readiness gates, private UI/CSV labels, and
  snapshot invalidation on manifest/roster changes without resetting the form feed.
- Final full suite: **1,249 passed, 1 skipped in 265.44s**. Python compilation, JavaScript syntax,
  CLI help and diff whitespace checks passed. No current service restart or deployment was tested.
- Final 600-draw experiment: 200/300/400/600/800 stations, strengths 0/1/2/3, 30 seeds each,
  using 779676 snapshot built 2026-11-07T23:16:12Z (8,385 usable / 8,392 stations).
- The smallest passing in-sample design was 200 stations, **zero early-speed tilt**: p95 full-sample
  maximum share error 0.846pp; p90 count-80% time 7.850h and balanced release 9.600h, relative to
  earliest stored original upload, not polls closing. Its predeclared proposal had 0.330pp full error
  and 9.550h balanced release. Stronger early preference improved count arrival but not balanced
  release in this dataset. Do not claim the requested early-speed improvement was validated.
- Artifacts: `/tmp/opencode/rg2-historical-design-20261108-balanced.json` and companion
  `.stations.csv`. No upcoming sample activated; existing 1,600-station pilot preserved. All source
  database access was read-only; no paid analysis, publication, staging, commit or push.
- Remaining scientific gaps: incomplete prior numeric reference, same-election design/readiness
  evaluation, unverified numeric as-of availability, mapping for an upcoming roster, and independent
  national/presidential validation. Forecast intervals remain unavailable for the pivotal design.

## Confirmed activation for 779676 (8 November 2026)

The operator explicitly confirmed replacing the active 1,600-station pilot with the new 200-station
proposal and rebuilding the private snapshot. Preserved the old manifest byte-for-byte at
`data/election-779676.sample-1600-backup-20261108.json`; atomically replaced
`data/election-779676.sample.json` with the validated 200-station retrospective manifest.
Rebuilt the private snapshot through the normal locked publisher with watcher-status writes disabled:
200/200 selected stations usable, status experimental, no coverage blockers. Focused verification:
35 historical-design/operator-estimate tests passed. No source DB writes, public export, service
restart, staging or commit. No upcoming election has been activated.

## Approved explanatory panel and sampling intervals

Add the exact frozen sample's prior full-sample reconstruction error and the matching design's
30-trial p95 full-sample error to an explanatory panel, not a forecast margin. Bind optional trial
metadata to the source fingerprint/design, retain partial-reference and same-election warnings,
and show station response plus weighted prior-electorate response as completeness, not accuracy.

Use the conservative Hansen–Hurwitz variance estimator for pivotal sampling, applied by sampling
group to weighted ratio-linearization residuals. See Chauvet & Ruiz-Gazen (2016), section 4,
equation (9), https://arxiv.org/html/1609.02688v2. This avoids unidentifiable zero joint-inclusion
terms and avoids incorrectly reusing the SRS finite-population variance. Report approximate,
pointwise 95% conditional sampling intervals and margins, not validated forecast intervals.
Incomplete response additionally assumes exchangeable response within each group; the prior-balance
release gate/optional timing and ratio linearization prevent an unconditional 95% coverage claim.
Do not turn zero observed non-census residual variation into a zero-width certainty claim.

Preserve active station membership/weights and all-arrivals/public calculations. Verify formulas,
synthetic randomization behavior, UI labels/escaping and provenance; rebuild the private snapshot
without restarting services or reanalysing forms. Leave changes unstaged/uncommitted.

### Verification and activation status

- Implemented the historical explanatory panel, completeness measures, approximate pointwise HH
  intervals/margins, zero-variation handling, and source/design-bound optional trial metadata.
- Full suite: **1,258 passed, 1 skipped in 257.40s**. Focused estimate/UI/public checks:
  195 passed. Python compilation, JavaScript syntax and diff whitespace checks passed.
- Synthetic behavior-only experiment: 250 draws each at 40 and 80 stations, with unequal
  probabilities; empirical coverage 94.8% and 97.6%, mean interval widths 17.61pp and 12.49pp.
  This is not independent election validation. Results in
  `/tmp/opencode/rg2-sampling-interval-synthetic-20261108.json`.
- Added matching 30-trial metadata to the active 200-station manifest, preserving all other fields;
  previous bytes backed up to `data/election-779676.sample-before-intervals-20261108.json`.
- Built and verified the full **separate preview** at
  `/tmp/opencode/rg2-sample-interval-preview-20261108/index.html`: 200/200 reporting, correct historical
  errors (0.33pp exact sample, 0.85pp design p95), and nonzero sampling intervals. Verified counted
  totals, all-arrivals results/intervals and sample point estimates unchanged at the same revision.
- Live activation is blocked by the normal operator resource-generation guard: the running server
  has the pre-edit code generation loaded. An attempted generation check stopped before any
  snapshot publication. Did not bypass the guard, publish a mismatched live snapshot, or restart
  services. One explicit-approved server restart is required to load the new calculation and
  publish a matching live snapshot. No commit or push.

### Live activation completed after explicit restart approval

The operator approved SIGINT of serve PID 3302259 and relaunch with
`uv run rg2watch serve --db data/election-779676.sqlite --config rg2watch.toml`.
Executed those actions. Verified over HTTP at `http://127.0.0.1:8770` that the rebuilt
snapshot (2026-11-08T08:21:42Z) has 200/200 reporting, `conditional_95_pivotal_hh` intervals
with positive margins, the 30-trial historical benchmark, matching data/meta snapshot IDs,
and the current resource generation. The new explanatory panel HTML is served too.

## Corrected priority: election-night readiness, not merely a small sample

The operator reiterated that the fixed historical sample should report fairly quickly, on election
night if possible. First display historical first/50%/80%/90%/95%/last upload milestones in the
private sample panel, explicitly in Belgrade time and not as a forecast. Preserve expanded notes.
Then assess midnight/02:00 cutoffs using frozen prior profiles and compare speed against political,
geographic and electorate balance. Do not choose a neutral sample merely because it is smaller.
Retain current membership while assessing alternatives; no silent replacement or weaker gates.
Distinguish a national probability sample from a calibrated early-only/nonprobability experiment;
do not transfer HH sampling intervals to a method they do not justify. No paid analysis, external
fetch, public deployment, service restart or commit is required for the investigation.

### Timing display and tightening findings

- Timing table implemented client-side from frozen `prior_first_upload` fields, including missing
  timestamp accounting and explicit Belgrade dates; copied updated static page and verified over
  HTTP. No backend restart or sample change needed. Notes still retain expansion across refreshes.
- 320 additional full-support probability draws across 16 prior-political-stratum designs: none
  ready by midnight/02:00 under the existing count, weighted-response and prior-balance gates.
- Separate **nonprobability diagnostic**: fixed prior-balanced 200-station set with all historical
  uploads by 23:00; 80% by 22:44. Exact prior fitting residual 0.058pp, worst of five predeclared
  seeds 0.064pp. Foreign domain excluded (1.578% of prior electorate). These are source fitting
  residuals, NOT future accuracy or grounds for HH intervals. Candidate CSV saved separately.
- Active 200-station probability manifest unchanged; methodological tradeoff must be discussed
  before replacing it with an early-only model. No paid analysis, source DB writes, public export,
  service restart, commit or push.
- Verification for the timing change: **179 focused tests passed**, JavaScript/diff checks passed,
  and a real Chromium check using the actual 200-station snapshot confirmed the displayed times
  and retained expansion after a new snapshot. Full Python suite not rerun for this client-only
  display change. Both completed studies are diagnostic-only artifacts, not activated methods.

## Approved two speed-focused samples

The operator approved two separate fixed, purposively prior-balanced 200-station sets: Quick
(all prior original uploads by 23:00 election night) and Morning (by 08:00 the next morning),
both in Europe/Belgrade time. These are explicitly nonprobability experimental estimates, without
the pivotal/HH sampling intervals. Completion targets refer to all selected stations, not an 80%
release gate; future timing is not guaranteed. Preserve the existing quick feasibility membership
using its predeclared seed/optimizer and retain the former probability sample as a backup.

Implement a checksummed bundle containing frozen prior inputs and two independent memberships,
a read-only offline builder and explicit matching-roster future activation, two private sample
views/CSVs, prior fitting/coverage/timing information, and no fake inclusion probabilities. Preserve
all-arrivals/counting/public behavior and expanded-note state. Readiness may still hold partial
returns for poor prior balance; missing members are never replaced. Test formulas, manifests,
scope/roster isolation, future candidate counts and UI. A live backend restart will need separate
approval; build/verify a separate preview without bypassing resource-generation guards.

### Two-sample implementation verification

- Implemented deterministic deadline-constrained two-set builder, checksummed version-3 bundle,
  matching-roster future activation, independent Quick/Morning private views/CSVs, explicit
  nonprobability/no-interval disclosure, eligibility/domain/fit/timing information, and retained
  expanded-note state. Public and all-arrivals calculations remain unchanged.
- Full suite: **1,274 passed, 1 skipped in 254.03s**. Latest focused checks: 258 passed.
  Python compilation, JavaScript syntax and diff checks passed.
- Actual frozen profiles: Quick exactly matches the previously proposed set; all uploads by
  23:00. Morning all uploads by 07:08, before the 08:00 target. Both 200/200 currently usable,
  overlapping at four stations. Source fitting residuals 0.058pp / 0.031pp are not forecast accuracy.
- Real Chromium isolated preview using the actual stored numeric readings passed both tab,
  deadline/timing, no-HH/no-margin, CSV and expansion-retention checks. Its injected candidate
  payload is a preview, not evidence of live activation. Preview at
  `/tmp/opencode/rg2-two-speed-samples-preview-20261108.html`.
- Saved validated bundle to `data/election-779676.speed-samples-20261108.json` and preserved the
  current probability manifest byte-for-byte at
  `data/election-779676.sample-probability-backup-20261108.json`. Active `.sample.json` remains
  unchanged until the separately approved backend restart/activation. No source DB writes,
  network fetching/reanalysis, public publication, staging or commit.

### Two-sample live activation completed

After explicit operator approval, stopped serve PID 3343617 with SIGINT, atomically replaced
`data/election-779676.sample.json` with the validated speed bundle, and relaunched
`uv run rg2watch serve --db data/election-779676.sqlite --config rg2watch.toml`.
The previous probability sample remains byte-for-byte in its backup. Verified over HTTP that
the live snapshot built 2026-11-08T09:06:07Z has Quick and Morning at 200/200, both experimental
with `not_probability_sample`, no intervals/margins or blockers, matching data/meta snapshot IDs,
the current resource generation, and unchanged all-arrivals output. New two-sample HTML is served.

## Final operator readability and commit request

Speed views now show three brief status/timing/caution lines and the results, with technical
information under a collapsed Method & details section. Blockers stay visible; expanded notes
remain open across refreshes. Client-only verification: 180 focused tests and a real Chromium
visibility/expansion check passed; updated HTML was served without restarting the operator's watch
service. No estimation math or sample membership changed for this UI simplification.

The operator subsequently explicitly requested committing the accumulated sample/projection work.
Final review stages only the associated source, tests and documentation. Ignored local election
databases, snapshots, active manifests, backups and experiment outputs remain local and unchanged;
no push or service action is included in the commit request.

Final pre-commit verification: **1,275 passed, 1 skipped in 269.10s**. Python compilation,
JavaScript syntax and staged-diff whitespace checks also passed.
