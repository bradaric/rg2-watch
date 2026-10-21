# rg2-watch runbook

Operator steps for election night and for targeted checks afterwards.
See the [README](../README.md) for installation, [Analysis](analysis.md) for models and evaluation,
[Review and sharing](review-and-sharing.md) for verdicts and case handoff, and
[Runtime reference](reference.md) for server behavior, throttling and privacy.

All commands run from the repo root. `--env-file .env` loads the Anthropic key (and the ntfy topic,
if you put it there); commands that never call Claude do not need it.

## Quick start

One argument: the election ID (digits). Databases are `data/election-<id>.sqlite`.

- `scripts/night.sh <id>` - election night: register if needed, watch with analysis (`--watch-only`, `--baseline` if starting late)
- `scripts/rehearse.sh <id>` - rehearsal on a past election, runs until done
- `scripts/view.sh <id>` - rebuild the page and serve it (no RIK, no API)
- `scripts/status.sh <id>` - compact status: counts, spend, review state, watcher running or not
- `scripts/escalated.sh <id>` - forms marked ESCALATE
- `scripts/tally.sh <id>` - unofficial totals
- `scripts/second-opinion.sh <id>` - re-analyse with local `ollama:bonsai-2:27b` (Ollama must be running)

Extra arguments are passed through to the underlying command. An older rehearsal DB can be renamed to
fit: `mv data/rehearsal-677087.sqlite data/election-677087.sqlite`.

## 1. Days before

- [ ] `uv sync`, then `uv run pytest -q` - all pass.
- [ ] Anthropic credit: enough for the election (section 6), plus margin.
- [ ] ntfy: install the ntfy phone app, subscribe to an unguessable topic (the topic name is the
      only secret). Add `RG2W_NTFY_TOPIC=<topic>` to `.env`. Test it:
      `curl -d "rg2-watch test" https://ntfy.sh/<topic>` - the phone must buzz.
- [ ] Config: `cp rg2watch.example.toml rg2watch.toml` and set:

      model = "claude-haiku-5-5"       # or claude-sonnet-5-5 / claude-opus-5-5 / ollama:<name>
      # prices come from a built-in table by model (Haiku 0.10/0.50, Sonnet 2/10, Opus 4/20 USD per
      # MTok, local 0/0); set price_in_per_mtok/price_out_per_mtok only to override it, or for a
      # model outside the table (which is refused without them)
      max_spend_usd = 60.0             # per election; a parliamentary election is ~$40 on Haiku
      watchlist = []                   # municipalities/stations to check first (priority, not a filter)

- [ ] Rehearsal against the fake RIK (no load on the real site, no API cost without `--analyze`):

      uv run rg2watch replay --fixtures data/fixtures/662405 --speed 600 --faults
      uv run rg2watch add http://127.0.0.1:8765/zapisnici/662405 --db /tmp/replay.sqlite
      uv run rg2watch watch --parent 662405 --base-url http://127.0.0.1:8765 --db /tmp/replay.sqlite

  You should see new, replaced and removed documents and a synthetic RG-9 alert.

## 2. Election night

1. When the election's page exists on RIK (`https://www.rik.parlament.gov.rs/zapisnici/<parent>`),
   register it and bootstrap the municipality and station lists:

       uv run rg2watch add https://www.rik.parlament.gov.rs/zapisnici/<parent> --config rg2watch.toml
       uv run rg2watch watch --parent <parent> --config rg2watch.toml --once

2. Start the live loop in a terminal that survives a closed laptop lid / SSH drop (`tmux new -s rg2`):

       uv run --env-file .env rg2watch watch --parent <parent> --config rg2watch.toml --analyze

   (or `scripts/night.sh <parent>`)

   - Leave out `--analyze` to only watch for new/replaced/removed documents (free).
   - Started late, with forms already published? Add `--baseline`: what is already there is
     recorded silently, only later changes alert. Already-published RG-2s are still analysed.
3. Open the URL `watch` prints (`page: http://127.0.0.1:PORT`, normally 8770; the next free port if
   busy; `rg2watch serve` prints it too when not watching): a live status panel, then every RG-2 with its alerts, second opinions, filters and
   PDF links. The table refreshes every 2 minutes and the panel every 10 s, without a reload.
4. What arrives on the phone:
   - HIGH, immediately (at most 10 individual pushes per 5 minutes; beyond that one "[HIGH] N
     findings" digest when the window allows, same compact format; status alerts such as RIK
     blind, analysis paused or spend cap are never held back by that backlog; more than 20
     replaced documents in one sweep give ONE "mass replacement" alert and a digest, not 20
     pushes; `high_push_max`, `high_push_window_s`, `mass_replace_threshold`): corrected numbers, more ballots than voters, very high outside voting (15%+),
     RG-8/RG-9 published, an RG-2 replaced or removed, the watcher itself blind or blocked.
   - MEDIUM, every 10 minutes as one digest: missing signatures, outside voting 10-15%, committee
     remarks, unreadable fields, arithmetic mismatches.
   - LOW: terminal and page only.
5. For each alert: open the PDF link and check it yourself. Alerts are leads, not conclusions.

Stop with Ctrl-C (waits up to 80 s for analyses in flight). Restarting the same command resumes
from the database without re-alerting.

## 3. When something goes wrong

| Symptom | What it means | Do |
|---|---|---|
| "RIK unreachable/blocking since HH:MM" | RIK down, overloaded or refusing us | Nothing; it retries and backs off. "reachable again" follows. If it lasts, check the site in a browser. |
| "analysis paused: ..." | Key, credit or model problem | Fix it (top up credit, check `.env`), then wait; it retries itself after 10 min. |
| "spend cap reached" | `max_spend_usd` hit | Raise it in `rg2watch.toml`, restart `watch`. Watching continued meanwhile. |
| "analysis delayed: Anthropic overloaded" | Anthropic busy | Nothing; forms are retried without losing attempts. |
| An active form's PDF link answers 404 | RIK listed the document before its PDF became available | Nothing; `watch --analyze` retries the uncached PDF every 120 s without using analysis attempts or creating D3, and resumes automatically when it appears. Browser links may still show 404 meanwhile. |
| "PDF downloads blocked" | RIK refusing downloads (consecutive 401/403), or at least 10 cumulative download deferrals | Nothing; downloads recover automatically. A 401/403 streak pauses downloads; an HTTP 404 instead breaks that streak. Listing changes are still detected. |
| Forms stuck as failed (X2/D3/X1, LOW) | Analysis gave up on them | After fixing the cause: `uv run --env-file .env rg2watch requeue --parent <parent> --failed`, then keep `watch --analyze` running. |
| Watch started without `--analyze` | Forms recorded, not analysed | Restart with `--analyze`, and `uv run rg2watch requeue --parent <parent> --skipped`. |

Each election has its own page folder (`data/election-<id>/`, next to `data/election-<id>.sqlite`), so
two elections can run side by side (e.g. `watch` on one, `scripts/view.sh` on another, on different
ports). Files directly in `data/` (`index.html`, `data.*`, `status.*`, libraries) are from the old
shared layout and safe to delete.

## 4. Targeted checks (during or after the night)

- **Find forms by issue, municipality or station:** the page (the URL `watch` or
  `rg2watch serve` printed) filters by municipality, model, severity, rule,
  replaced/removed, "models disagree" and free text. Rebuild it any time:

      uv run rg2watch browse --config rg2watch.toml

- **One form, a second opinion** (e.g. with Opus, or a local model for free):

      uv run --env-file .env rg2watch analyze <RIK PDF URL> --config rg2watch.toml --model claude-opus-5-5
      uv run rg2watch analyze <RIK PDF URL> --config rg2watch.toml --model ollama:qwen3.8:27b-64k

  Prints the findings and the cost. The URL must be on the RIK host of a registered election.
- **A local PDF:** `uv run --env-file .env rg2watch analyze path/to/form.pdf`.
- **Review the alerts on the night:** in the page choose review "to review" (forms with findings and
  no verdict yet; the status panel counts them). For each: open the row (arrow), open the PDF,
  then OK (false alarm or legitimate correction), ESCALATE (send to the desk) or UNSURE, with a
  short note. Verdicts are saved at once (only over http, not from an opened file) and survive
  restarts; a replaced form comes back as "to review". Hand over with "Escalated CSV", or:

      uv run rg2watch review --list escalated --config rg2watch.toml

   (or `scripts/escalated.sh <id>`)
      uv run rg2watch review <RIK PDF URL> escalate --note "10.1 corrected, call RIK" --config rg2watch.toml

  Afterwards, turn the verdicts into eval label suggestions (a new file; merge by hand):

      uv run rg2watch labels --from-reviews --out data/review-labels.jsonl --config rg2watch.toml

- **Free re-score after changing a threshold** (e.g. outside voting): `eval --cached` on the
  golden set re-applies the rules to stored analyses without calling any model.

## 5. Rehearse on a past election (full run against RIK)

A full dress rehearsal: watcher, analysis, page, phone alerts and review, on an election whose
forms are already published (e.g. local 2025, `662405`). Use a separate DB so it never mixes with
election-night data.

1. Register and start (keep `RG2W_NTFY_TOPIC` in `.env` to practise the pushes and digests too):

       uv run rg2watch add https://www.rik.parlament.gov.rs/zapisnici/662405 --config rg2watch.toml --db data/rehearsal-662405.sqlite
       uv run --env-file .env rg2watch watch --parent 662405 --config rg2watch.toml --db data/rehearsal-662405.sqlite --analyze --baseline --until-done

   (or `scripts/rehearse.sh <id>`, which uses `data/election-<id>.sqlite`)

   `--baseline` records what is already published without "new document" alerts; every RG-2 is
   still downloaded and analysed, and its findings alert as on the night. `--until-done` makes
   the watcher exit by itself (exit 0) once every lane has completed a full pass and the analysis
   queue is empty: it pushes the last alerts (MEDIUM digest included), writes the final page and
   prints a summary (documents by type, RG-2 analysed/failed/skipped, findings by severity, spend,
   elapsed time). If analysis is stuck for 10 minutes (spend cap, key error, downloads blocked,
   Anthropic overloaded) it exits 1 and says why and how many forms remain: fix the cause and rerun.
2. Open the URL it prints: watch the status panel (queue, spend, ETA), the forms filling in,
   the Results tab and presentation mode. List names come from `list_names` under `"662405"`.
3. Practise the review: filter "to review", open PDFs, record OK / ESCALATE / UNSURE, export the
   escalated CSV.
4. The summary ends with the browse command (Ctrl-C still stops the watcher early; the status
   panel shows the queue meanwhile). Browse the result afterwards with:

       uv run rg2watch serve --config rg2watch.toml --db data/rehearsal-662405.sqlite

   (or `scripts/view.sh <id>`)

5. Optional free second opinion with a local model, then filter "models disagree":

       uv run rg2watch analyze --parent 662405 --model ollama:bonsai-2:27b --config rg2watch.toml --db data/rehearsal-662405.sqlite

   (or `scripts/second-opinion.sh <id>`)

Expect ~100-150 RG-2s for 662405: well under $1 on Haiku, about 5-10 minutes (PDF downloads run
at 0.5/s). This does load RIK lightly (listing sweeps at up to 2 requests/s while it runs), so do
not leave it running for hours. Start from an empty DB to repeat it.

## 6. Cost and speed (estimates, 20/10/2026)

Measured on 72 RG-2s from past local elections (RIK zapisnici 662405 and others), Anthropic list
prices as of 19/10/2026. Election size: about 250-450 RG-2s for a local election, about 8,300 for a
parliamentary one.

| Model | Per form | Local election | Parliamentary | Keeps up? |
|---|---|---|---|---|
| Haiku 5.5 (default) | ~$0.005 (measured) | ~$1.50-2.50 | ~$40 | yes |
| Sonnet 5.5 | ~$0.06 (measured) | ~$15-30 | ~$530 | yes (4 workers in parallel) |
| Opus 5.5 | ~$0.13 (5-form pilot) | ~$30-60 | ~$1,100 | yes |
| Local (Qwen) | $0, ~78 s | 5-10 h | ~1 week | local elections only |

Calibration 20/10/2026: Haiku 5.5 matched Sonnet 5.5 on 72 RG-2s; Bonsai (local) recommended as a
free second opinion afterwards (caught the small rotated scan both missed).

## Watcher behavior and recovery details

HIGH findings (RG-8/RG-9 published, RG-2 replaced, with the old URL as "was: ...") are pushed
immediately, MEDIUM in digests, LOW only printed. Individual HIGH pushes are capped at
`high_push_max` (10) per `high_push_window_s` (300 s); the rest roll into one "[HIGH] N findings"
digest (same compact format) sent when the window allows, so a busy night cannot trip ntfy's rate
limits. Status alerts (RIK unreachable, analysis paused, spend cap) are separate, go first and are
never held back by the finding backlog. If one sweep replaces more than `mass_replace_threshold`
(20) documents, ONE "[rg2-watch] mass replacement: N documents replaced in one sweep (RIK
re-upload?)" alert is pushed and those D2 findings go to the digest.
Alerts are delivered, and RIK reachability checked, by their own thread every 5 s, so a slow
RIK tick (10 minutes at a throttled rate) never holds them back.
A second `watch` on the same database is refused (exit 2, `<db>.lock` holds the pid of the first). A digest is compact (under 3.5 KB): counts by rule, up
to 10 detail lines without URLs, and "+K more - open the page, filter MEDIUM" with the page URL when
serving (a single-finding digest keeps its RIK URL). Every finding is also printed (and flushed) to
stdout. If ntfy is unreachable, findings stay un-alerted and pushes back off (5 s doubling to
60 s) before being retried. Ctrl-C or SIGTERM stops cleanly; state is kept in the SQLite DB
(`data/rg2watch.sqlite` by default, `--db` to change), so a restart resumes without re-alerting.

If RIK answers 401/403 repeatedly, all lanes pause for `blocked_pause_s`. After `blind_alert_s`
without a successful request you get one HIGH "[rg2-watch] RIK unreachable/blocking since HH:MM"
push, and "[rg2-watch] RIK reachable again" on recovery.

Starting mid-night on an empty DB? Add `--baseline`: each lane's first full pass records what is
already published without alerting, prints one `[baseline] <type>: N document(s) recorded` line
per document type, and later changes alert normally.
On a DB that already holds documents of the election `--baseline` is refused (exit 2) unless
`--force-baseline` is given; `scripts/rehearse.sh` only passes it on an empty DB.
Only care about some places? `--only "Beograd,Нови Сад"` (or `only_municipalities` in the config;
Cyrillic or Latin, case-insensitive, exact name beats substring) limits every lane to those
municipalities: no global listings are requested, other documents are not recorded, and an
unknown or ambiguous name exits 2 with the candidates. Per-municipality counts replace the global
ones, so a larger set costs more requests per round.

## Replay (rehearsal without RIK)

Download a past election once (polite default 1 request/s; PDFs optional):

    uv run rg2watch fixtures https://www.rik.parlament.gov.rs/zapisnici/662405 --out fixtures [--pdfs --limit-pdfs 50]

Re-enact it locally with a fake RIK server (documents appear along their original upload times,
`--speed` is simulated seconds per real second, `--faults` injects a replace, a removal and a synthetic RG-9):

    uv run rg2watch replay --fixtures fixtures/662405 --speed 600 --faults
    uv run rg2watch add http://127.0.0.1:8765/zapisnici/662405 --db /tmp/replay.sqlite
    uv run rg2watch watch --parent 662405 --base-url http://127.0.0.1:8765 --db /tmp/replay.sqlite
