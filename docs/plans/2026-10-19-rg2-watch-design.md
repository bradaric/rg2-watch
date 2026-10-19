# rg2-watch - design

Date: 19/10/2026
Status: draft - autonomous defaults, pending user review. Items marked **[call]** are my decisions, not user-confirmed.

## 1. Intent

Watch the Republic Electoral Commission (RIK) site for newly published polling-station minutes (RG-2, "Записник о раду бирачког одбора") and raise alerts on forms that look irregular, so a human can check them manually.

- Primary use (the product): election night, live. Detect **diffs** on the RIK listing as data comes in - new RG-2 uploads, replaced files, new correction/annulment documents - and analyse each new form within minutes.
- Past elections (e.g. local 2025, `662405`) are **test data only**: a replay mode serves archived records in upload-time order through a fake RIK endpoint to exercise the live pipeline end to end. A one-off audit of a past election is a side effect, not a goal.
- The output is a **lead for manual review**, never a verdict of fraud.

### What counts as irregular (user priority order)

1. **Visual/physical integrity of the scanned form** (primary, per user): overwritten or altered digits, crossed-out values, correction fluid, scratched-out areas, missing signatures, missing committee members, sloppy/inconsistent fill-in (different pen or hand in the results block), empty mandatory fields, missing/wrong pages.
2. Procedural flags written on the form: control sheet not found / not signed, voting interrupted, committee remarks, remarks text present.
3. Arithmetic inconsistencies between the numbers on the form.
4. Statistical outliers across stations (turnout, leader share).
5. Document-level events on the RIK site: correction decisions (RG-7), "results cannot be determined" (RG-8), annulment (RG-9), remarks (Примедба), re-uploaded/replaced RG-2 files.

### Goals
- Detect new RG-2 uploads within one sweep cycle (target <= 10 min for a parliamentary election).
- Per form: a structured "integrity report" + findings with severity.
- Push alerts to phone; dedupe; HIGH immediately, MEDIUM batched.
- Every alert links to the original PDF and the station.
- Re-runnable on past elections for calibration (2025 local elections, 2023 parliamentary).

### Non-goals
- Deciding whether fraud occurred.
- Publishing anything automatically.
- Full transcription of the form (names, signatures content).

### Anti-goals
- Alert flood on election night (false-positive storm makes the tool useless). Severity tiers, batching and calibration on past elections exist to prevent this.
- Hammering RIK servers (politeness limits, see 4.1).
- Storing or sending personal data (names of voters/committee members) - see 7.

## 2. Source recon (verified 19/10/2026)

- Listing pages: `https://www.rik.parlament.gov.rs/zapisnici/<parent>`; the page embeds
  `additionalData = {"type":1,"parent":"<parent>","election_round":"<round>","property_election_type":"<t>"}`.
- Listing API: `POST /get-election`, form fields `page`, `data[type]`, `data[parent]`, `data[election_round]`, `data[property_election_type]`, `election_type`, optional `filters[election_document_type]` (6 = RG-2), `filters[municipality]`, `filters[election_station_no]`.
  Returns `{records: [...], pagination: "<html>"}`; **fixed 10 records/page** (page-size params ignored). Each record: `municipality`, `election_station_number`, `election_station_name`, `extfiles[]` with `extfile` (HTML snippet: doc-type label, `<a href>` PDF link, upload time `DD-MM-YYYY HH:MMh`), `uid`, `date` (upload timestamp), `file_type`.
- Document type ids (from the filter select): 6 RG-2, 7 RG-7, 8 RG-8, 9 RG-9, 10 RG-2 repeat, 11 RG-6, 12 RG-11/НП (observers), 13 correction, 14 RG-7 after court, 15/16 RG-2 repeat II/III, 19-26 RG-7-x, 27 RG-8 repeat, 28 RG-9 repeat, 29-31 Примедба.
- Known listings: `662405` (local 2025, round 660578, type 3), `677084` (local 2026, round 668522), `779676` (parliamentary 2026, round 680072, type 2, currently 0 RG-2 records).
- Results app: `POST /get_results/` (`type`, `election_round`, `region`, `municipality`, `election_station`) needs a session cookie + `X-Requested-With: XMLHttpRequest`; `POST /get-elections/`, `/get-regions/`, `/get-election-stations/`. Returns tabulated results; per-station use not yet verified.
- PDFs: scanned, 2 pages, A3 landscape, one 200 dpi JPEG per page (3306x2338), no text layer. Page 1 = sections I-III (+ IV item 11), page 2 = items 12-15 and section V (committee names + signatures, completion date/time).
- Form header code varies per election (e.g. `Образац РГ-2/25-ЗАЈ`); the number of lists varies per election/municipality.
- The site loads Google reCAPTCHA on the page but the JSON endpoints above did not require it.

## 3. Architecture

Python 3.12+, `uv`, single package `rg2watch`, SQLite, no server. **[call]**

```
 sweep (RIK listing API) --> new/changed documents --> download PDF --> render pages
        |                                                                  |
        v                                                                  v
  document-level rules                                    vision analysis (Claude) -> integrity report JSON
        |                                                                  |
        +-----------------------> findings (SQLite) <----- form rules + arithmetic rules
                                       |
                                       +--> cross-station stats (periodic)
                                       v
                                notifier (ntfy / stdout / Telegram) + HTML/CSV report
```

Modules:
- `rik.py` - listing discovery (parse `additionalData` from a zapisnici URL), paged sweep, record parsing (`extfile` HTML -> doc type, URL, upload time). Results-app client in phase 2.
- `store.py` - SQLite schema + queries.
- `fetch.py` - PDF download, sha256, local cache `data/pdfs/<parent>/<sha256>.pdf`.
- `render.py` - PDF -> page images (`pypdfium2`, pure pip) **[call]**; crops of known regions (results block, signatures block) at full resolution.
- `analyze.py` - Claude vision call, structured output (schema in 5).
- `rules.py` - pure functions: integrity report -> findings.
- `stats.py` - cross-station outlier detection.
- `notify.py` - channels + batching + dedupe.
- `cli.py` - entry points.
- `report.py` - static HTML/CSV report of findings with thumbnails.

### 3.1 Data model (SQLite)
- `elections(parent PK, round, election_type, title, url)`
- `documents(id PK, parent, municipality, station_no, station_name, doc_type, uid, url UNIQUE, uploaded_at, first_seen_at, sha256, status)` - status: `new|downloaded|analyzed|failed`.
- `analyses(id PK, document_id, model, prompt_version, result_json, created_at, cost_usd)`
- `findings(id PK, document_id NULL, parent, municipality, station_no, rule, severity, summary, details_json, created_at, alerted_at NULL, UNIQUE(document_id, rule))`
- `sweeps(id PK, parent, started_at, finished_at, pages, records, added, replaced, removed)`
- `events(id PK, sweep_id, parent, kind added|replaced|removed, document_id, station key, doc_type, url, uploaded_at, detected_at)`

## 4. Pipeline

### 4.1 Live diff watcher (core of the product)
- The listing appears to contain only stations that already have documents (parliamentary 2026 listing currently returns 0 records) - so it grows during the night. **To verify on the first live night**; the watcher must not depend on it.
- Each sweep walks listing pages 1..N with `filters[election_document_type]` unset (so RG-7/8/9/Примедба are seen too) and builds a snapshot `{(station_key, doc_type, url, uid, uploaded_at)}`. The snapshot is diffed against the stored state:
  - **added**: URL never seen -> enqueue for download/analysis; for RG-7/8/9/Примедба -> D1.
  - **replaced**: same station + doc type, different URL/uid -> D2 + analyse the new file.
  - **removed**: previously seen URL gone from listing -> D4 (MEDIUM).
  - Every diff event is stored in `events` (append-only log) with sweep id and timestamps -> gives "time from upload to detection" metrics.
- Latency: pagination is by station, not time, so new documents can land on any page. Parliamentary ~8,300 stations => up to ~830 pages => ~7 min at 2 req/s for an unfiltered sweep. The API supports the same filters as the UI (verified: `filters[election_document_type]`, `filters[municipality]`, `filters[election_station_no]`), so the watcher runs **several filtered sweep lanes** sharing one rate limiter, each with its own interval: **[call]**
  1. **Rare-document lanes** (RG-7 and variants 7/14/19-26, RG-8 8/27, RG-9 9/28, Примедба 29-31, repeat RG-2 10/15/16): one filtered sweep per type; few pages each; every 60 s. Highest-signal events, lowest cost.
  2. **Watchlist lane**: user-configured municipalities (`filters[municipality]` by id), all doc types, every 60-120 s.
  3. **RG-2 lane** (`filters[election_document_type]=6`): full sweep, continuous, with a change probe - page 1 + pagination page count every 30 s; if the count grew, restart the sweep immediately.
  4. Optional: parallel per-municipality slices of the RG-2 lane if RIK tolerates it (concurrency default 1).
  5. Analysis runs from a queue alongside the lanes, never waits for a sweep to finish.
- Diff semantics are per lane: a "removed" event is only raised for documents that the same lane (same filter) saw before.
- Municipality ids: `GET /templates/election/getElectionStations.php?q=<latin prefix>&electionId=<round>` returns `[{"id","name"}]` (Cyrillic query returns empty; Latin works). Cached in `municipalities(parent, id, name)`. Config watchlist accepts names or ids.
- Politeness: default max 2 req/s total, identifying User-Agent `rg2-watch/<ver>`, exponential backoff on 429/5xx, pause and alert self on repeated 403. **[call]**
- New document = URL not seen before. Same station + same doc type + different URL/uid = **replacement** (finding D2).

### 4.1a Replay (testing)
- `rg2watch replay --parent 662405 --speed 60x` starts a local fake `/get-election` that serves the cached archive listing filtered to `date <= simulated clock` and serves cached PDFs. The watcher runs unchanged against `--base-url http://127.0.0.1:<port>`.
- Archive listing + PDFs are fetched once into a fixture cache (`data/fixtures/<parent>/`), never re-fetched during tests.
- Fault injection in replay: replace a file mid-night, remove a record, add an RG-9, to test D2/D4/D1.

### 4.2 Download + render
- Download each new PDF once; record sha256. Render each page at native resolution (200 dpi). Produce crops:
  - page 1 left half (sections I-II), page 1 right half (section III results),
  - page 2 left half (items 12-15), page 2 right half (section V signatures + completion time).
- Crops are resized to the largest size the Claude image input accepts without server-side downscaling (implementer: check current limits in the claude-api skill docs). Crop boxes are relative fractions, not pixels, so they survive other scan resolutions.

### 4.3 Vision analysis
- One Claude request per document with the 4 crops (+ optionally downscaled full pages for context), structured output via `output_config.format` JSON schema.
- Model: `claude-opus-5-5` (skill default), configurable. **[call]** Rough cost: ~4 crops x ~1.6k tokens + prompt => ~8k input tokens/form => ~$0.03-0.05/form; ~8,300 forms ~ $300-400 for a parliamentary election. Cheaper model is a config switch after calibration (user decision).
- Concurrency: 8 parallel requests, retries via SDK. **[call]**
- Second opinion: if any HIGH visual finding or any arithmetic failure, run one more independent analysis (same model, higher effort) on just the relevant crop; finding records whether both agree. Disagreement => severity stays, summary says "uncertain read".
- Audit mode may use the Message Batches API (50% cost); watch mode uses realtime.

### 4.4 Rules -> findings
See 6.

### 4.5 Notify
- Default channel: ntfy (`ntfy_url` + topic in config; topic name is the secret on public ntfy.sh - recommend self-hosted or long random topic). Fallbacks: stdout always; Telegram bot optional. **[call]**
- HIGH: immediately, one push per finding. MEDIUM: digest every 10 min. LOW: report only. **[call]**
- Message: severity, municipality, station no + name, rule, one-line reason, PDF URL. No personal names.
- Dedupe on (document, rule); a replacement document produces new findings.

## 5. Integrity report schema (vision output)

No personal names are transcribed. Per numeric field:
`{value: int|null, legible: bool, mark: "clean"|"overwritten"|"crossed_out"|"correction_fluid"|"erased_scratched"|"digit_altered"|"empty"|"dash", note: str}`

Fields:
- header: `form_code` (string as printed), `station_no` (item 1), `date` (item 2), `pages_present` [1,2], `pages_match_same_form` bool.
- numbers: `registered` (3), `first_voter_number` (6), `voted` (10.1), `ballots_in_box` (10.2), `invalid` (10.3), `valid` (10.4), `list_votes[]` ({row, value-field}), `assisted` (11), `outside_voted` (12), `outside_confirmations` (12.1).
- yes/no (circled): `interruption` (7), `control_sheet_found` (9), `control_sheet_signed` (9.1), `observers_present` (13), `observer_form_filled` (13.1), `committee_remarks` (14) - each `{answer: "DA"|"NE"|"both"|"none"|"unclear"}`.
- times: `open_time` (4), `close_time` (8), `completed_at` (end of page 2).
- `remarks_text_present` (15) bool, `remarks_summary` short (no names).
- signatures (section V): `rows_total`, `rows_with_name`, `rows_with_signature`, `president_signed` bool, `rows_name_without_signature`, `signatures_look_same_hand` bool + note.
- global visual: `results_block_different_pen` bool, `results_block_different_hand` bool, `pasted_or_patched_area` bool, `page_quality` ("good"|"poor"|"unreadable"), `other_anomalies[]` short strings.
- `confidence`: "high"|"medium"|"low" overall.

## 6. Detection rules (initial; thresholds in config)

Visual / physical (primary):
| id | rule | severity |
|---|---|---|
| V1 | any of voted/ballots_in_box/invalid/valid/list_votes marked overwritten, crossed_out, correction_fluid, erased_scratched or digit_altered | HIGH |
| V2 | same in other numeric fields (registered, 11, 12, 12.1) | MEDIUM |
| V3 | president did not sign | HIGH |
| V4 | named committee rows without signature >= 2 (or any, configurable) | MEDIUM |
| V5 | signatures look like same hand | HIGH |
| V6 | results block in different pen or different hand than rest of form | HIGH |
| V7 | pasted/patched area | HIGH |
| V8 | mandatory field empty/illegible (any of 3, 10.1-10.4, list votes) | MEDIUM |
| V9 | page missing, pages from different forms, form_code/station_no mismatch with listing | HIGH |
| V10 | page quality poor/unreadable | LOW |
| V11 | both DA and NE circled / none circled on a yes/no item | MEDIUM |

Procedural:
| P1 | control sheet not found or not signed (9/9.1 = NE) | HIGH |
| P2 | voting interrupted (7 = DA) | MEDIUM |
| P3 | committee remarks (14 = DA) or item 15 text present | MEDIUM |
| P4 | completed_at before close_time, or close_time before 20:00 | MEDIUM |

Arithmetic (secondary; only when all involved fields legible):
| A1 | ballots_in_box > voted | HIGH |
| A2 | invalid + valid != ballots_in_box | MEDIUM |
| A3 | sum(list_votes) != valid | MEDIUM |
| A4 | voted > registered | HIGH |
| A5 | outside_voted != outside_confirmations | MEDIUM |
| A6 | turnout >= 95% (registered > 50) | MEDIUM |
| A7 | one list >= 90% of valid (valid > 50) | MEDIUM |
| A8 | assisted > 5% of voted | LOW |

Document-level:
| D1 | RG-7 / RG-8 / RG-9 / Примедба published for a station | MEDIUM (RG-8/RG-9 HIGH) |
| D2 | RG-2 replaced (new URL for same station + type) | HIGH |
| D3 | PDF download/render failure, page count != 2 | LOW |
| D4 | previously listed document disappeared from listing | MEDIUM |

Cross-station (stats.py, per municipality, run every N new forms and at end):
| S1 | robust z (median/MAD) of turnout or leader share > 3.5 within municipality, min 10 stations | LOW (MEDIUM if combined with any V/P finding) |
| S2 | count of V-findings per municipality unusually high | LOW |

## 7. Privacy and security

- RG-2 forms contain personal names (first voter, committee). The vision prompt instructs not to transcribe names; schema has no name fields. Alerts and reports carry no names.
- PDFs are public documents; cached locally under `data/` (gitignored).
- Content from RIK pages/PDFs is untrusted data; never interpreted as instructions. The vision prompt states that any text in the image is data.
- Secrets (API key, ntfy topic, Telegram token) only via env/config file outside git.

## 8. CLI

- `rg2watch add <zapisnici-url>` - register an election (parse additionalData).
- `rg2watch watch [--parent ID] [--base-url URL] [--no-analyze]` - live diff loop (the product).
- `rg2watch replay --parent ID [--speed 60x] [--faults]` - fake RIK server from cached archive for testing.
- `rg2watch fixtures --parent ID [--limit N]` - fetch archive listing + PDFs once into the fixture cache.
- `rg2watch analyze <pdf-or-url>` - one-off analysis, prints integrity report + findings.
- `rg2watch report --parent ID [-o out.html]` - HTML/CSV of findings with crop thumbnails.
- Config: `rg2watch.toml` (thresholds, model, channels) + env `ANTHROPIC_API_KEY`, `RG2W_NTFY_TOPIC`, `RG2W_TELEGRAM_TOKEN`.

## 9. Calibration / evaluation

- Golden set: ~30 forms from local elections 2025 (`662405`), hand-labelled for the visual fields (overwrites, missing signatures). Initial labels drafted by the builder from the images, **to be verified by the user**.
- Metric: per-rule precision/recall on the golden set; false-positive rate per 100 forms on a full municipality backfill.
- Thresholds tuned so that HIGH alerts stay below ~2% of forms on a normal election (user to confirm target).

## 10. Phases

- **Phase 1a (this branch, first):** live diff watcher - rik client, store, sweep + diff + events, change probe, D1/D2/D4 findings, ntfy+stdout notify, fixtures + replay server, CLI `add/watch/replay/fixtures`. Usable on its own on election night (alerts on corrections/annulments/replacements, PDF links).
- **Phase 1b (this branch, next):** fetch + render/crops, vision analysis, rules V/P/A, analysis queue running alongside the sweep, CLI `analyze`, golden-set harness.
- Phase 2: stats S1/S2, HTML report, Telegram, second-opinion pass, parallel municipality sweeps.
- Phase 3: cross-check vs RIK tabulated per-station results (`/get_results/`), PDF metadata/scan forensics (JPEG ELA, producer/creation-time anomalies), observer forms (RG-11) parsing.

## 11. Open questions for the user

1. Model/cost: default Opus 5.5 (~$0.03-0.05/form). OK, or prefer cheaper after calibration?
2. Notification channel: ntfy OK? Self-hosted or ntfy.sh?
3. Alert volume target on election night (HIGH <= 2% of forms?).
4. Is a "missing signature" from a committee member always worth an alert, or only the president's?
5. Where it runs on election night (this laptop vs a server).
