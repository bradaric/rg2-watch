# Experimental operator-only national estimates

The private operator page supports **Quick sample**, **Morning sample** and **All-arrivals projection** tabs.
None is included in public exports. Counted results, holds, corrections and selection of
repeat-vote forms remain unchanged. These are experiments, not validated early-result forecasts.

## Two speed-focused historical samples (approved workflow)

The approved speed design is a bundle of two separate fixed **200-station purposive samples**, fitted using
only a frozen completed PRIOR election's results, electorate sizes, geographic cells and upload times:

- **Quick:** all selected prior original uploads by **23:00** on the source election's first-upload
  calendar day in Europe/Belgrade.
- **Morning:** all selected prior original uploads by **08:00 the following morning**.

These deadlines concern **all selected stations**, not the 80% estimate-readiness gate. They are
historical selection constraints, **not future upload or analysis-ready guarantees**. Upcoming returns
never select or replace members. The two sets can overlap and share source data; agreement between
them is not independent corroboration.

Both deliberately exclude late/incomplete historical profiles. They are **not national probability
samples**, have no known national inclusion probabilities, and show **no probability-based sampling
interval or ± margin**. Prior fitting errors are explicitly not independent accuracy measurements.
The previously implemented SRS/pivotal designs remain supported for comparisons/backups only.

Build a new reproducible bundle without any database writes, network access or reanalysis:

```sh
uv run rg2watch estimate-speed-samples \
  --history-sample data/election-779676.sample.json --size 200 \
  --seed rg2watch-night-feasibility-20261108:0 \
  --out data/election-779676.speed-proposal.json
```

The source manifest must contain validated frozen prior profiles (version 2 or 3). The offline
builder uses 40,000 deterministic seeded swap attempts per set to reduce normalized prior national
vote-share residuals, prior mean electorate/ballot-size deviations and geographic electorate-share
deviations. It is heuristic in-sample constraint fitting, not a proof of optimality or a lucky-seed
search. Unknown votes are never filled with zero. Both sets, their source profiles, deadlines, seed,
frame/source hashes and optimizer settings are frozen in a checksummed version-3 bundle.

For a future election with an explicitly verified identical roster/boundaries:

```sh
uv run rg2watch estimate-sample --db data/election-<upcoming-id>.sqlite --parent <upcoming-id> \
  --history-design data/election-779676.speed-proposal.json \
  --seed rg2watch-night-feasibility-20261108:0 --confirm-station-mapping
```

Activation copies both memberships unchanged and refuses mismatched rosters or pre-existing output
files. Same-election activation must be explicitly retrospective. Manual replacement of the existing
779676 comparison manifest requires the confirmed backup/activation operation; the CLI never silently
overwrites it.

For each sample, live shares are the ratio of the **sum of usable selected current votes** to the
**sum of selected valid + invalid ballots**. There are no invented `1/pi` weights or transferred
historical list/candidate votes. Missing/held/excluded selected readings remain missing. Partial
estimates are withheld below 80% of selected count or selected prior-electorate mass, or when the
reporting subset's prior political/geographic residual exceeds 2pp/3pp. These are model safeguards,
not an accuracy guarantee. Even 200/200 returned is labelled an experimental sample, not a complete
national census.

Each view has its own CSV, membership/status, historical timing and fit/eligible-pool disclosures.
The default speed-sample view is deliberately brief: stations received/update time, target/prior
completion time, and one short experimental/no-margin caution, followed immediately by the results.
Technical metadata, fitting diagnostics, coverage limitations, timing tables and station lists are
under a collapsed **Method & details** section. User-expanded sections remain open across updates;
estimate blockers remain visible rather than being hidden with the technical explanation.
The all-arrivals tab and its existing nominal conditional intervals remain unchanged.

### Built speed sets for source 779676

Using the frozen 8,385 usable / 8,392-station source reference and the predeclared feasibility seed:

| Set | Eligible known-profile pool | Prior electorate in eligible pool | Last selected original upload | Prior full-set vote fitting residual |
|---|---:|---:|---|---:|
| Quick | 2,132 | 14.37% | 23:00, 25 October 2026 | 0.058pp |
| Morning | 8,158 | 96.29% | 07:08, 26 October 2026 | 0.031pp |

All times are Europe/Belgrade. Quick membership exactly matches the previously proposed 23:00 set;
the two sets overlap at **four stations**. Both lack the foreign domain (1.578% of prior electorate).
The Morning count excludes seven uploaded stations without usable prior numeric profiles. The tiny
residuals are fitted on the same prior votes, **not forecast margins or independent validation**.

## Prior probability-sample design (retained comparison)

The requested sample is designed **before the upcoming election**, using a completed prior
election's station results, electorate sizes and earliest stored original-upload times. Upcoming votes and
their arrival order never choose membership. The original 1,600-station geography-only pilot below
was a mistaken interpretation of this requirement; it is retained only as a labelled comparison.

### 1. Design and test the fixed station list now

Select the historical source explicitly. This example uses completed election **779676** as the
source for a **later** election; it is not a prospective design for election 779676 itself:

```sh
uv run rg2watch estimate-design \
  --history-db data/election-779676.sqlite --history-parent 779676 \
  --history-snapshot data/election-779676/data.json \
  --geography ordered --sizes 200,300,400,600,800 --early-strengths 0,1,2,3 --trials 30 \
  --seed rg2watch-prior-20261108 \
  --out data/prior-design-20261108.json --stations-out data/prior-design-20261108.stations.csv
```

The command reuses stored numbers and opens the prior SQLite database read-only. No PDF fetching,
RIK requests, model calls, reanalysis or upcoming-election results are involved. JSON and optional
formula-escaped station CSV outputs must be new paths. The report freezes the source inputs,
selection probabilities, design weights, source fingerprint and predeclared proposal seed.

The method uses **ordered pivotal probability sampling**, not “pick whichever forms arrive first”:

- Geographic ordering uses fixed coarse latitude/longitude cells, assigned from each
  municipality's median cached station coordinate. Foreign, institutional and unlocated stations
  retain explicit domain labels. These are sampling cells, not official regional boundaries
  or an urban/rural classification.
- The default is **one national fixed-size probability draw**, ordered by geographic cell,
  prior political vote profile and size. This provides implicit geographic/political spreading,
  not a hard requirement to obtain a form from every municipality or tiny special domain.
  Every station remains eligible with a known positive probability, including late-reporting domains.
- Use strictly positive scores proportional to prior electorate size times
  `exp(-early_strength * historical_upload_rank)`. Zero means no early-time preference, not the
  absence of a historical size/political design. Late stations always retain a positive chance.
- Capped PPS probabilities sum to the fixed size. Ordered pivotal draws preserve these
  inclusion probabilities and the exact sample size. Actual design weights are
  **`1 / inclusion_probability`**, not simply `stratum stations / selected stations`.
- Missing prior electorate readings use explicitly flagged median size proxies for **selection
  scores only**. Missing prior vote profiles remain eligible; they are not invented or filled with zero.

The report compares predeclared sizes/preferences across multiple seeds. It measures weighted
historical vote-share errors, design-weighted prior electorate/geographic balance, time to 80%/90%
usable sample returns, and release
time and the fraction of draws that can release. The provisional diagnostic targets are: at least
90% of draws release, 95th-percentile maximum historical share error <=1pp at full available returns,
<=2pp at release, 95th-percentile **design-weighted** prior electorate-total relative error <=5%,
and maximum geographic prior-electorate-share error <=3pp at its 95th percentile. Electorate balance
uses the original `1/pi` weights against known prior electorate totals, not current-election
response-calibrated totals. Among passing designs,
prefer the smallest size, then earlier release. If none pass, the report explicitly says so; a
fallback proposal is only diagnostic, not a validated recommendation; activation refuses a report
whose diagnostic targets failed. Quantiles describe released
draws; withheld draws remain in the release-rate denominator.

The actual proposed list uses the separately predeclared seed, **not the luckiest trial seed**.
Its own historical error/timing diagnostics are reported too. Do not repeatedly change seeds until
the prior result happens to match. Designing/tuning/testing on the same prior election is in-sample
design work, not independent validation of future accuracy. A partial prior reference is explicitly
identified; its errors are against known stored votes, not certified final national results.

`--geography grid` and `--geography municipal` retain hard geographic/municipal strata as comparisons
(two selected/reporting stations per non-singleton stratum, with historical-electorate allocation).
Both measured experiments exposed why hard domain quotas were unsuitable for early estimates:
mandatory returns from a small late-reporting domain could keep the release gate shut for days even
when most of the sample had reported. The default national probability design does not impose those
quotas; instead its **actual design-weighted geographic balance** is checked on prior data.

### 2. Freeze against the upcoming roster, without reselection

Once the upcoming election ID and complete roster exist, verify that historical and upcoming
station identities **and boundaries** match. Then activate the frozen proposal:

```sh
uv run rg2watch estimate-sample --db data/election-<upcoming-id>.sqlite --parent <upcoming-id> \
  --history-design data/prior-design-20261108.json --seed rg2watch-prior-20261108 \
  --confirm-station-mapping
```

This copies the **same selected stations and probabilities**, not a new draw from incoming forms.
The seed must match the proposal; an explicit size, if supplied, must match too. A changed roster
is refused rather than silently mapped/resampled. Identical identifiers alone cannot establish
unchanged boundaries: the confirmation is an operator assertion, not an automatic validation.
If station splits/merges or identity changes exist, resolve mapping/design before activation.

Activation creates `data/election-<upcoming-id>.sample.json`, never overwriting an existing sample.
An upcoming station's unavailable reading remains missing; another early station never replaces it.
If results already exist locally, activation requires the explicit retrospective flag and retains
that label. The source election cannot be activated as its own prospective target.

A prior parliamentary political profile may be used as auxiliary information for a future
presidential station sample without mapping list numbers onto presidential candidates. That does
**not** establish presidential-specific forecast accuracy; an independent presidential validation
is still required. Historical source and target election are always recorded separately.

### 3. Live estimation from only the frozen historical sample

Within each actual frozen sampling/response group, let `b_i = 1/pi_i` and normalize reporting weights to its
roster population: `w_i = N_h*b_i / sum_reporting(b_i)`. Calculate national shares from weighted
current vote totals divided by weighted `valid + invalid` ballots. This is a calibrated ratio
estimator, not a claim of exact finite-sample unbiasedness. Nonresponse still requires missing
selected stations to resemble reporting stations within the response group. With the default single
national draw the common scale cancels from national shares, leaving the ratio of inverse-probability
weighted vote and ballot totals.

Release requires **both** at least 80% of selected stations usable and at least 80% of the selected
**design-weighted prior-electorate mass** returned. The latter uses frozen prior sizes (explicit
proxies where unknown), not newly observed sizes or a claim about current electorate coverage.
This prevents many low-weight early returns from hiding a few missing high-weight stations.
At least two usable stations are required per actual non-singleton response group; the default
single national group does **not** require a return from every geographic domain or municipality.
Known inclusion probabilities do not remove
nonresponse, station-mapping, extraction or future-change risks.

There is also a frozen **prior-profile readiness check**: the reporting selected stations, weighted
as they would be live but using only their prior-election votes, must reproduce the known prior
national vote shares within 2pp and prior geographic electorate shares within 3pp. Unknown prior
vote profiles may represent at most 5% of the returned weighted prior-size mass. These policy values
are recorded in the manifest and validated with it. This check only withholds an estimate; it never
adds, removes or replaces selected stations, and never compares upcoming vote shares to prior ones.
It can withhold even a fully returned sample that is poorly balanced on prior data. A genuine complete
current-election census does not need this prior-profile gate.

**The replay's release error is consequently a balance-gate diagnostic, not an accuracy backtest:**
the same prior votes inform the design, readiness check and reference. A <=2pp prior release error
is partly enforced by construction. Ungated full-sample errors still include all trial draws, not
only released ones. Independent election-to-election validation remains missing; no policy is a
validated future error bound.

The old simple-random-stratum variance formula is **not valid for this pivotal design**. The
historical sample now uses the conservative Hansen–Hurwitz approach below for **approximate
sampling intervals**, not a validated forecast interval. Historical design errors are not
relabelled as current-election confidence intervals. CSV includes sampling stratum, actual inclusion
probability, original design weight, response-adjusted weight and historical source election.
The tab also shows frozen historical geographic balance and the prior-weighted response fraction.
Creating or changing a manifest/roster invalidates the derived estimate snapshot on the next builder
refresh even when there are no new votes; it does not needlessly reset the live forms feed.

### Explanatory panel and approximate sampling intervals

The panel separates three quantities:

1. **Historical benchmark:** maximum prior list-share error for the entire fixed sample's available
   prior readings, plus the matching design's p95 maximum full-sample error across trial draws when
   recorded evidence is present. Both use frozen prior inputs, not the current reporting subset.
   Optional trial metadata travels in `design_diagnostics`, bound to source fingerprint, size and
   early preference; activation preserves it. No trial evidence means “unavailable,” never zero.
   These are same-election design/reconstruction checks, **not future margins or independent validation**.
2. **Completeness:** returned selected-station fraction and returned design-weighted prior-electorate
   proxy. Neither is a measure of current accuracy or actual current electorate coverage.
3. **Approximate pointwise 95% sampling interval and margin:** calculated per current list/candidate
   using only selected, eligible **current** readings. It does not reuse historical candidate/list
   values as variance data or map old parliamentary lists onto new presidential candidates.

For each response group, with `r_h` usable stations, `w_i` the existing calibrated inverse-inclusion
weight, `B_i = valid + invalid`, national estimated share `p`, and `D = sum_i w_i B_i`, define:

```text
z_i = w_i (votes_i - p B_i)
V_h = r_h / (r_h - 1) * sum_reporting (z_i - mean_h(z))^2
SE(p) = sqrt(sum_h V_h) / D
interval = [max(0, p - 1.96 SE), min(1, p + 1.96 SE)]
margin_pp = 100 * 1.96 SE
```

Groups whose entire roster is usable contribute zero sampling variance. Other groups use no SRS
finite-population correction: [Chauvet & Ruiz-Gazen (2016), section 4, equation (9)](https://arxiv.org/html/1609.02688v2)
shows the HH variance estimator is conservative in expectation for pivotal **totals**, including
when some joint inclusion probabilities are zero. Applying it to estimated ratio residuals and
estimated response-calibration factors remains an approximation, not a finite-sample coverage proof.
It may be substantially wider than retrospective reconstruction errors; this is not a reason to
substitute those errors as a margin.

Incomplete response additionally assumes exchangeable response within each sampling group.
Readiness gating, repeated checking, ratio linearization, small/unequal effective sample sizes,
and normal approximation mean **unconditional 95% forecast coverage is not established**. Intervals
are individual-list/candidate, not simultaneous across all rows. They exclude systematic reporting
bias, station mapping changes and extraction/reading errors. Displayed margins are untruncated
normal-approximation margins; interval endpoints are clipped to [0%, 100%].

Zero observed non-census residual variation gives no interval/margin, not a false zero-width band;
the experimental point estimate remains available. A fully usable national census has zero sampling
variance but can still contain reading errors. Existing readiness gates, sample membership/weights,
all-arrivals calculations and public results are unchanged.

## Shared numeric inputs and the all-arrivals comparison

### Corrected historical-design experiment (8 November 2026)

The private sample tab now has an expandable **Historical reporting times** table: first known,
50%, 80%, 90%, 95% and last selected original upload, formatted in **Europe/Belgrade** time with
calendar dates. It uses the frozen sample's recorded prior timestamps; unknown times are not
silently dropped from percentile denominators. This is not a future timing forecast or a measure
of analysis availability. Expanded timing notes survive automatic updates.

#### Election-night tightening investigation

The operator's objective is election-night readiness, not simply the smallest sample. A separate
read-only study tested 16 political-bin probability designs (5/10 prior leading-list-share bins,
200/400 stations, early strengths 1/2/3/4), 20 seeds each. **None of those 320 draws passed the
unchanged readiness gates by midnight or 02:00 local time.** This is a result for those tested
designs, not proof that every possible probability design must fail.

An explicitly different **early-only, purposively prior-balanced** feasibility check used only
stations with prior original uploads by 23:00, midnight or 02:00, then fitted a fixed 100/200-station
subset to prior national list-share residuals, electorate/ballot sizes and geographic electorate
shares using deterministic seeded swap optimization. No current/upcoming returns were inputs.
Each combination used five predeclared seeds; the displayed proposal uses seed 0, not the best seed.

For the **200-station 23:00** feasibility set:

- Historical uploads: first 20:46; 50% 22:18; 80% 22:44; 90% 22:53; 95% 22:56; all 200 by 23:00.
- Proposed set's prior maximum list-share reconstruction error: 0.058pp; worst over the five fitted
  sets: 0.064pp. Prior mean electorate-size relative error: 0.054% for the proposal.
- The foreign geographic domain was absent from the eligible early pool; its prior electorate
  share was 1.578%, which is also the largest geographic share discrepancy. This set does not
  directly cover that domain.
- Those tiny vote errors are **in-sample fitting residuals**, not an accuracy backtest. Later
  stations have no chance of selection, so this is **not the current national probability design**.
  The pivotal/HH intervals must not be carried over. Future timing, political shifts and excluded
  groups require model assumptions and independent validation.

Reports: `/tmp/opencode/rg2-early-political-study-20261108.json` and
`/tmp/opencode/rg2-early-only-feasibility-20261108.json`; diagnostic station list:
`/tmp/opencode/rg2-early-balanced-200-2300-20261108.csv`. At that investigation stage no replacement
had been activated. The operator subsequently approved the two speed-focused samples described
above, including removal of inapplicable probability-sampling intervals.

Final report: `/tmp/opencode/rg2-historical-design-20261108-balanced.json`; fixed proposal CSV:
`/tmp/opencode/rg2-historical-design-20261108-balanced.stations.csv`. These are local design
artifacts, **not an activated upcoming-election sample**. With explicit operator confirmation, the
200-station probability proposal was initially activated for **779676 as a retrospective sample**.
It was later backed up at `data/election-779676.sample-probability-backup-20261108.json` when the
Quick/Morning bundle replaced it at `data/election-779676.sample.json`. The earlier 1,600-station
manifest remains at `data/election-779676.sample-1600-backup-20261108.json`.

Source: election 779676, explicit operator snapshot built **7 November 2026, 23:16:12 UTC**,
8,385 usable / 8,392 roster stations. Tested 200/300/400/600/800 stations, four early preferences,
30 seeds per combination. All times below are elapsed from the earliest stored original upload
(25 October 2026, 19:38 UTC), **not from polls closing**.

| Size | Early preference | p95 full-sample maximum share error | p90 time to 80% station count | p90 balanced release |
|---:|---:|---:|---:|---:|
| 200 | 0 | 0.846pp | 7.850h | 9.600h |
| 200 | 1 | 1.133pp | 7.017h | 9.883h |
| 200 | 3 | 1.786pp | 5.833h | 10.150h |
| 400 | 1 | 0.582pp | 7.017h | 9.533h |
| 600 | 1 | 0.488pp | 6.917h | 9.250h |
| 800 | 0 | 0.289pp | 7.667h | 9.167h |

The smallest design meeting the provisional in-sample targets was **200 stations with zero early
preference**, releasing in 30/30 trials. It still uses prior electorate/geographic/political inputs;
zero means no additional reporting-speed tilt. The separately predeclared 200-station proposal had
0.330pp full-sample maximum share error, reached 80% count at 7.650h and balanced release at 9.550h.

The early tilt successfully selected quicker stations, but **did not improve balanced release time**
in this stored dataset: missing late, high-weight or politically different stations still mattered.
Do not describe the experiment as establishing an early-optimized forecast or as evidence that
200 stations guarantee future precision. Release errors are gated using those same prior votes,
so their <=2pp values are not independent accuracy evidence. No independent national/presidential
election-to-election validation or live upcoming-election activation was performed.

Both methods use corrected station readings from the same complete tally snapshot. Escalated,
annulled, undetermined, incomplete and numerically impossible results are missing returns, not zeros.
Visual flags alone do not remove an otherwise eligible reading. The sample never substitutes an
early reporting station for a missing selected one.

The **all-arrivals projection and legacy geography-only sample** divide the stored roster by
municipality. For each municipality, multiply received
**vote totals** and ballot totals by `roster stations / usable reporting stations`, then calculate
national shares from the expanded totals. This respects different ballot counts between reporting
stations, but assumes that reporting and missing stations within a municipality resemble one another.
It does **not** calibrate on pre-election electorate size, urban/rural labels or previous voting.
Station coverage is known; electorate coverage is not.

Shares use **valid + invalid ballots**, matching the existing counted-results view. They are not
valid-vote-only shares, statutory thresholds, presidential majority calculations, seat allocations,
winner calls or runoff eligibility decisions. National parliamentary and presidential ballots can
use the same method; each election gets its own roster and sample. A presidential runoff must be
treated as a separate election, not validated by a first-round test.

Municipal and council scopes are unsupported. When RIK's type is ambiguous (including historical
presidential records), the existing explicit scope override is required in the operator's TOML:

```toml
# Top-level key, before any [table]. Use the actual election ID.
tally_scope = { "288987" = "national" }
```

## Legacy geography-only sample (comparison only)

Before results exist, after the complete election-specific station roster has been stored:

```sh
uv run rg2watch estimate-sample --db data/election-<id>.sqlite \
  --parent <id> --size 1600 --seed 'a-predeclared-seed'
```

Pass the usual `--config rg2watch.toml` if a scope override is needed. This command opens SQLite
read-only and creates **`data/election-<id>.sample.json`**, next to the database. It never overwrites
an existing manifest. The operator builder automatically reads that exact file on its next snapshot;
it is not served as a separate downloadable JSON file. The tab offers a formula-escaped station CSV,
including membership, reporting status, original design weights and current response-adjusted weights.

Selection depends only on the frame, size and seed, not results or input ordering. A stable
pseudo-random hash ranking selects stations within each municipality. Allocate at least two per
non-singleton municipality (one for certainty singletons), then distribute additional slots
proportionally to remaining capacity using largest remainders. The minimum size therefore depends
on the roster; the current pilot needs at least **371** stations. Allocation is by station count,
not electorate share. The manifest fingerprints the entire roster. Changed membership, weights,
seed or roster are refused rather than silently repaired/resampled.

If any RG-2 already exists locally, creation requires `--retrospective`. That flag stays visible in
the operator tab. An after-the-fact sample is useful for experiments but is not evidence of
prospective registration. The file is not a tamper-proof public registration; preserve the original
manifest and seed externally if independent verification of advance selection is needed. Do not
search seeds until one happens to fit known results.

**1,600 was the original comparison size, not the corrected historical-design recommendation or a
precision guarantee.** Without `--history-design`, `estimate-sample` still creates that legacy
geography-only comparison. Smaller municipal elections
are intentionally not supported. A national roster smaller than the requested sample is refused;
specify a valid smaller size rather than relying on silent capping.

## Legacy/all-arrivals coverage and nominal intervals

No national estimate is shown while any municipality has fewer than two usable returns, except
single-station municipalities, which require their one return. Sample estimates also require at
least **80% of selected stations** reporting overall. These gates are conservative diagnostics,
not empirically validated accuracy cutoffs. Missing or stale manifests disable only the sample
estimate, not counted results or the separate all-arrivals projection.

For the legacy sample/all-arrivals comparison only, the interval uses stratified station-cluster
ratio linearization and a finite-population correction:

```
p = sum_h (N_h/r_h) sum_i votes_hi / sum_h (N_h/r_h) sum_i ballots_hi
e_hi = votes_hi - p * ballots_hi
Var(p) ≈ sum_h N_h² (1-r_h/N_h) s²(e_h)/r_h / expanded_ballots²
interval = p ± 1.96 * sqrt(Var(p)), clipped to [0,1]
```

It is a **pointwise, normal-approximation conditional 95% interval**, not simultaneous coverage
for every candidate/list. Fully returned prospectively randomized samples have a sampling-design
interpretation. Partial samples and nonrandom all-arrivals require exchangeable reporting within
municipalities. No interval includes systematic arrival bias, extraction errors or unknown frame
omissions. Absent non-census variance produces unavailable intervals; when all observed variances
are zero, withhold the estimate instead of claiming perfect certainty. A full usable roster has no
sampling error, but may still have reading errors.

The tabs display their snapshot timestamp and an Updating warning when form revisions are newer.
They do not respond to counted-results clean/as-filed/municipality filters: these are national,
corrected estimates, avoiding accidental changes of target population.

## Offline replay without reanalysis

```sh
uv run rg2watch estimate-replay --db data/election-<id>.sqlite --parent <id> \
  --snapshot data/election-<id>/data.json --trials 50 \
  --sizes 100,200,400,800,1200,1600 --out /path/to/new-replay-report.json
```

No RIK requests, PDF downloads or model/API calls. No database changes. `--snapshot` explicitly
reuses saved operator numeric results and checks the database/election and document identities.
Its source timestamp is recorded; old snapshots are not silently treated as current. Omitting it
rebuilds the corrected tally read-only, which can be slow on a large database. Reports never
overwrite an existing output file. Without `--out`, print the summary only.

At up to 25 distinct upload cutoffs, compare raw received totals, all-arrivals projection and one
fixed sample (largest admissible requested size, first trial seed). Timestamp ties enter together;
naive RIK times are interpreted in Europe/Belgrade. Missing/malformed times are reported, not replaced
with the historical import's first-seen time. Size experiments use deterministic multiple seeds and
report the fraction of samples that pass the gates, error quantiles and pointwise interval coverage.

**This is an arrival-bias experiment, not an as-of/live-workflow replay.** It uses today's selected
forms, analyses, corrections and reviews. A final selected repeat-vote form does not appear until
its later upload time; its original station reading is not reconstructed. Analysis delays, replaced
versions and changes of decisions are not replayed. Final-state knowledge is an explicit limitation,
not evidence of real-time performance. Missing timestamps or incomplete reference coverage further
limit interpretation. The reference is stored usable votes, not independently verified official results.

When the reference is partial, errors are labelled diagnostics against that partial cohort and
±1/±2pp target assessments are disabled. A complete reference enables historical target counts, but
`accuracy_validated` remains false: tuning and testing on the same election is not out-of-sample
validation for a future election.

## Initial pilot (7 November 2026)

Source: election 779676's saved operator snapshot built **6 November 2026, 19:56:08 UTC**.
It contained **8,385 usable stations / 8,392 roster stations**, across 191 municipalities. Fifty
deterministic retrospective samples per size produced:

| Sample size | Trials passing coverage gates | 95th percentile of maximum list-share error | Worst released error |
|---:|---:|---:|---:|
| 100 / 200 | Refused: below 371-station minimum | — | — |
| 400 | 31 / 50 | 2.141 pp | 2.702 pp |
| 800 | 50 / 50 | 1.330 pp | 1.901 pp |
| 1,200 | 50 / 50 | 0.897 pp | 1.245 pp |
| 1,600 | 50 / 50 | 0.838 pp | 0.944 pp |

These are errors versus the **partial stored reference**, not independently verified national
errors. Withheld draws remain in the trial denominator; error quantiles describe released draws only.
For 1,600 stations, the mean pointwise nominal-interval coverage against that reference was only
**92.6%**, not 95%. Do not interpret the displayed nominal intervals as empirically calibrated
confidence guarantees. No interval multiplier was tuned on this same election to conceal that gap.
The JSON pilot report was saved outside the repository at
`/tmp/opencode/rg2-estimate-replay-20261107.json`; rerun the command to reproduce the experiment.
The final rerun, with the 1,600-station arrival timeline, is
`/tmp/opencode/rg2-estimate-replay-final-20261107.json` and reproduced the same size diagnostics.

The arrival timeline exposed a major limit: the all-arrivals coverage gate remained closed until
8,335 usable forms at the sampled cutoffs, and the first release was after election night. The
final-selected-repeat caveat matters here. **This first version is not validated as a useful early
national forecast.** More importantly, the raw first 209 forms differed by about 13.7 percentage
points from the eventual stored reference: an early pile of forms is not a representative sample.

Before relying on either method for the upcoming election, obtain a complete pre-election roster,
freeze the sample before results, resolve the missing reference readings, and validate on independent
parliamentary and presidential elections. Useful earlier estimates may require verified pre-election
electorate/size and geographic covariates and a separately tested missing-group model. Do not relax
the gates or invent precise intervals merely to display a number sooner.

A 1,600-station **retrospective pilot manifest** was created locally at
`data/election-779676.sample.json`, seed `rg2watch-pilot-20261107` (subsequently backed up to
`data/election-779676.sample-1600-backup-20261108.json` when the 200-station sample was activated).
Against the saved snapshot,
1,599 selected stations had usable returns. It is not a sample for an upcoming election and must
not be reused across election IDs or changed rosters. Existing running services and public exports
were not restarted or republished.

The stored 2022 presidential election currently has analyses for only **467 RG-2 documents across
72 municipalities** (the roster has 8,267 stations). Its older saved snapshot has no station-result
rows. It cannot provide an independent national presidential validation without additional verified
numeric data. No paid processing was initiated to fill that gap.
