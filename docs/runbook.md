# rg2-watch runbook

Operator steps for election night and for targeted checks afterwards. Reference detail for every
option is in README.md.

All commands run from the repo root. `--env-file .env` loads the Anthropic key (and the ntfy topic,
if you put it there); commands that never call Claude do not need it.

## 1. Days before

- [ ] `uv sync`, then `uv run pytest -q` - all pass.
- [ ] Anthropic credit: enough for the election (section 5), plus margin.
- [ ] ntfy: install the ntfy phone app, subscribe to an unguessable topic (the topic name is the
      only secret). Add `RG2W_NTFY_TOPIC=<topic>` to `.env`. Test it:
      `curl -d "rg2-watch test" https://ntfy.sh/<topic>` - the phone must buzz.
- [ ] Config: `cp rg2watch.example.toml rg2watch.toml` and set:

      model = "claude-sonnet-5-5"     # or claude-opus-5-5 / ollama:<name>
      price_in_per_mtok = 2.0          # must match the model: Sonnet 2/10, Opus 4/20, local 0/0
      price_out_per_mtok = 10.0
      max_spend_usd = 600.0            # per election; the default 20 stops a big election early
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

   - Leave out `--analyze` to only watch for new/replaced/removed documents (free).
   - Started late, with forms already published? Add `--baseline`: what is already there is
     recorded silently, only later changes alert. Already-published RG-2s are still analysed.
3. Open http://127.0.0.1:8770 in a browser (`watch` prints the URL; `rg2watch serve` serves it when
   not watching): a live status panel, then every RG-2 with its alerts, second opinions, filters and
   PDF links. The table refreshes every 2 minutes and the panel every 10 s, without a reload.
4. What arrives on the phone:
   - HIGH, immediately: corrected numbers, more ballots than voters, very high outside voting (15%+),
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
| "PDF downloads blocked" | RIK refusing downloads | Nothing; downloads pause and resume. Listing changes are still detected. |
| Forms stuck as failed (X2/D3/X1, LOW) | Analysis gave up on them | After fixing the cause: `uv run --env-file .env rg2watch requeue --parent <parent> --failed`, then keep `watch --analyze` running. |
| Watch started without `--analyze` | Forms recorded, not analysed | Restart with `--analyze`, and `uv run rg2watch requeue --parent <parent> --skipped`. |

## 4. Targeted checks (during or after the night)

- **Find forms by issue, municipality or station:** the page (http://127.0.0.1:8770, or
  `rg2watch serve` when not watching) filters by municipality, model, severity, rule,
  replaced/removed, "models disagree" and free text. Rebuild it any time:

      uv run rg2watch browse --config rg2watch.toml

- **One form, a second opinion** (e.g. with Opus, or a local model for free):

      uv run --env-file .env rg2watch analyze <RIK PDF URL> --config rg2watch.toml --model claude-opus-5-5
      uv run rg2watch analyze <RIK PDF URL> --config rg2watch.toml --model ollama:qwen3.8:27b-64k

  Prints the findings and the cost. The URL must be on the RIK host of a registered election.
- **A local PDF:** `uv run --env-file .env rg2watch analyze path/to/form.pdf`.
- **Free re-score after changing a threshold** (e.g. outside voting): `eval --cached` on the
  golden set re-applies the rules to stored analyses without calling any model.

## 5. Cost and speed (estimates, 20/10/2026)

Measured on 72 RG-2s from past local elections (RIK zapisnici 662405 and others), Anthropic list
prices as of 19/10/2026. Election size: about 250-450 RG-2s for a local election, about 8,300 for a
parliamentary one.

| Model | Per form | Local election | Parliamentary | Keeps up? |
|---|---|---|---|---|
| Sonnet 5.5 | ~$0.06 (measured) | ~$15-30 | ~$530 | yes (4 workers in parallel) |
| Opus 5.5 | ~$0.13 (5-form pilot) | ~$30-60 | ~$1,100 | yes |
| Local (Qwen) | $0, ~78 s | 5-10 h | ~1 week | local elections only |

## 6. Not supported yet

- Restricting the watch or the analysis to some municipalities (`watchlist` only puts them first).
- Re-analysing a batch (e.g. all of one municipality, or every form with a given alert) with
  another model in one command; today it is one `analyze` per form.
