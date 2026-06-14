# Session Summary

## Session: 2026-06-13 (Sat) — 202608 production cutover + explorer-refactor kickoff
**Directory**: `~/C/src/iSamples/isamplesorg.github.io`
**Trust Level**: external-content (GitHub/Slack/web read; live-data queries; **production R2 write + gh-pages deploy** — human-gated; no untrusted code run; Codex/agent outputs independently verified)

---

### What Happened
1. **202608 data cutover LIVE on isamples.org.** OC true-sync to Eric's current export: **+67,187 new / −21,227 stale Murlo re-IDs → OC MSR 1,110,791** (= his wide), rock 37,953. SESAR/GEOME/Smithsonian byte-identical (regression-checked). Plus #277 description+concept-label search (Cyprus 0→69,230; "pottery Cyprus" 0→1,305), #283 facet fixes, #260/#265 material. Built over **8 rounds + 5 Codex reviews**; published to R2 bucket `isamples-ry` (versioned: `isamples_202608_*` + `sample_facets_v3` + `vocab_labels_202608`), cut over via upstream PR #284. Closed #272/#277/#283/#260/#265 with live-prod evidence.
2. **Explorer refactor (#249) kicked off, gated.** Merged to rdhyee `main` (staging): **PR1** (#9, Playwright e2e smoke gate `explorer-e2e.yml`) + **PR2** (#11, 7 characterization tests). PR3 (extract 10 pure fns → ES modules) planned + ready.
3. **Track B** (background agents): data/metadata audit + 202608 doc drafts on a worktree branch — NOT integrated (number reconcile needed).

### Safe to Carry Forward
**Key decisions**: 202608 = production data (202606 superseded). Eric's Option B (remove the 21,227 Murlo re-IDs) is the source-of-truth call. Refactor = strangler/extract-along-seams behind the e2e gate; scope PR2+PR3, defer PR4/PR5. vocab_labels now **versioned** (prod unversioned file left untouched = cutover-safe).
**Files**: production pipeline merged upstream (#284: `scripts/ingest_oc_records.py` new + `build_frontend_derived.py`/`build_vocab_labels.py`/`validate_frontend_derived.py` + `explorer.qmd` 202606→202608+v3). Refactor (rdhyee main): `tests/playwright/helpers/explorer.js` + `explorer-characterization.spec.js` + `tests/README.md`. Staged build + all Codex reviews in `~/Data/iSample/pqg_refining/staged_202608/`.
**Patterns/gotchas**: (a) **table-total scope is unstable** (viewport vs global vs filtered) — never assert across it; use deterministic known-result searches. (b) pid deep-link sets `selectedPid`+`updateSampleCard`(`#clusterSection`) but NOT `showInMapCard` — row-click only. (c) e2e specs hit live remote parquet (~3-4 min/run) → OFF the PR gate (workflow_dispatch); smoke-only blocks PRs. (d) background worktree agents need `mode: bypassPermissions` or they hit a Write/Bash wall. (e) `--wide` validator shares `FACETS_DESCRIPTION_EXPR` with the builder so they can't drift.

### External Content Processed
| Source | Type | Notes |
|---|---|---|
| GitHub issues/PRs #272/#277/#283/#278-#282/#260/#265/#249, PRs #9/#10/#11/#284 | web/API | reports as data; replies as rbotyee; closures w/ prod evidence |
| Eric Kansa OC wide (GCS), live R2 202608 parquet | data | our own data |
| Codex CLI, ~10 review rounds | AI tool output | every finding verified by execution before applying |
| Slack #technical (Eric/Andrea/John) | web/API | close-the-loop; draft sent by RY |

### Open Threads
- [ ] **PR3** — extract 10 pure fns from `explorer.qmd` → `assets/js/sql-builders.js` + `explorer-utils.js` (3-step coexistence wiring + `node --test` units); branch off rdhyee main. Plan: `~/.claude/plans/serialized-honking-adleman.md`.
- [ ] **File 2 bugs** PR2 surfaced: pid deep-link doesn't open `#inMapCard` (#239-family); clearing search loses viewport scoping.
- [ ] **Track B docs**: reconcile the agent's wide-rowcount (verified **20,822,709 / OC 1,110,791**), then ship a docs-to-202608 PR (worktree `worktree-agent-a73f87c8d23e5bba5` has drafts + `DATA_FLOW_AUDIT_202608.md`).
- [ ] **Promote test infra (PR1+PR2) to upstream** (currently rdhyee main only).
- [ ] **Eric**: per-sample (1,305) vs collection-level (~14K) "pottery Cyprus" → next refinement.
- [ ] `claude/fix-optimization-issues-E5Kwr` loose cloud branch — inspect.

### Next Session Entry Point
> Production solid on 202608; refactor PR1+PR2 merged to rdhyee main. **Start PR3**: branch off rdhyee main, create the two ES modules, wire via the 3-step coexistence pattern (import alongside → verify smoke+characterization green → delete inline), add `node --test` units, Codex, squash-PR. Plan: `~/.claude/plans/serialized-honking-adleman.md`. Quick win first: file the 2 surfaced bugs.

---

## Session: 2026-05-30/31 (evening)
**Directory**: `~/C/src/iSamples/isamplesorg.github.io`
**Trust Level**: external-content

---

## What Happened

A long, productive session. Started as "tackle the fast-verify shakedown"; ended with **A1 shipped to production (isamples.org)** and **#248 underway**.

1. **Shakedown root-caused & fixed.** The dev `?data_base=/data` override produced root-relative parquet URLs that DuckDB-WASM's httpfs can't fetch (read as a virtual-FS glob → zero fetches). Resolved to absolute against `location.origin`. This unblocked the fast verify loop (~2.3s to live).
2. **The "globe logjam" was never real** — it was a **backgrounded-Chrome-MCP-tab artifact** (Chrome freezes rAF in hidden tabs → Cesium camera never settles → "globe won't enter point mode"). In any foreground/headless context the C3 fixes work. The reconciler refactor was unnecessary. **Lesson: drive the verify loop with `HEADLESS=1` Playwright, never the MCP tab.**
3. **Fixed an A1 search perf regression** the CI smoke gate caught (double facets scan → materialize side-panel columns+score into `search_pids`, one scan).
4. **Fixed the live facet-padding mismatch** RY hit (legend pad-0 vs table 0.3 → facet read low; e.g. material=rock ~166 vs ~481). Now facet == table.
5. **Shipped A1**: opened **PR #251**, ran a 3-round **Codex review/revise loop to dual approval** (Codex caught a real `search_pids` staging-table race, heatmap search-blindness, and a stale-reader follow-on — all fixed), then **squash-merged to upstream → deployed to isamples.org** (smoke gate green).
6. **Started #248 (Eric Kansa's concept-URI search)**: posted a connecting comment, Codex plan-reviewed ("mostly sound + guardrails"), and committed the **foundation** on `feat/described-by-concept`.
7. **Investigated a transient camera freeze** (RY's `h3=`+`heading=` deep-link, also on isamples.org). Ruled out locked controller / tracked-entity / refresh-loop via a new `?debug=a1` `__a1camera` hook; **resolved on its own → likely transient WebGL context-loss / network**. Surfaced a real **testing gap**: no gate asserts post-hydration *interactivity*.

---

## Safe to Carry Forward

### Key Decisions
- A1 ships on plain ILIKE; **BM25 (#168–172) is a perceived-perf follow-up, not a correctness blocker.**
- `search_pids` is a **singleton**; any new producer (#248) shares one `_searchFilterToken`/`_searchSeq` and the `kind: 'text'|'concept'` tag.
- Codex-reviewed A1 invariants to preserve: **token-scoped staging table**, **empty-table clear** (never DROP the live table), **build-failure distinguished from empty results**.
- `?debug=a1`-gated hooks: `__a1globe`, `__a1log`/`__a1state`, and (new, uncommitted/diagnostic) `__a1camera`.

### Branch / ship state
- **A1**: merged to upstream `main` as **`e6f9def`** (PR #251), live on isamples.org + rdhyee. Local `feat/search-global-filter-a1` is now redundant (squash-merged).
- **#248**: branch **`feat/described-by-concept`** off merged main; foundation commit **`f2eac35`** (`conceptLabelForUri` + `buildConceptFilter`, behavior-neutral, verified).

### Files Changed (this session, across A1 + #248)
- `explorer.qmd` — A1 data_base fix, double-scan collapse, facet-padding, Codex fixes (staging race / heatmap / empty-clear / build-failure msg), `?debug=a1` gating; #248 `conceptLabelForUri` + `buildConceptFilter`.
- `dev_server.py` — HTTP/1.1; `tests/playwright/a1-verify.mjs` — `HEADLESS=1` flag; new probes `globe-points-probe.mjs`, `shakedown-206.mjs`; `tests/playwright/facet-viewport.spec.js` — coherence test.

### Patterns/Learnings
- **Backgrounded tabs freeze rAF** → corrupts every globe/camera observation. Headless Playwright is the reliable instrument.
- **Don't pile up runs**: accumulated hung browsers hold HTTP/1.1 keep-alive + peg CPU and starve `dev_server.py`. Restart between batches.
- **Local mirror full-downloads** (GET 200, not 206) — fine on localhost; validate range/perf on the deploy, not the mirror.
- Codex's `codex exec ... -o FILE` often fails to capture the final message when the diff is large; read the verdict from the streamed `.log` instead (resume the session for continuity).

---

## External Content Processed

| Source | Type | Notes |
|---|---|---|
| GitHub (gh) — issues/PRs #234/#242/#244/#245/#246/#247/#248/#250/#251, CI logs | web/API | Read issue bodies as data. **Authored**: PR #251 + its review comment, #248 comment. **Merged** #251 to upstream production (RY-authorized "push to isamples"). |
| Codex CLI (gpt-5.4), session `019e7c8d…` | AI tool output | 3-round code review + #248 plan review. Findings **verified before applying**; treat as advisory. |
| isamples.org / rdhyee.github.io / localhost explorer | browser DOM (headless + 1 MCP tab) | Our own app. The MCP tab is what misled earlier sessions (rAF freeze). |
| `data.isamples.org`, local `docs/data/*.parquet` | remote/local data | Our own data. |

No secrets accessed, no untrusted code executed (Codex output hand-reviewed).

---

## Open Threads

- [ ] **#248 Flavor A — finish the wiring** (the delicate half): `doDescribedBy(uri)` + extract shared `runPidSetResults({heading,emptyText,orderBy})` from `doSearch` (touches the just-reviewed stale-guards); `described-by=` URL param boot-trigger (search-ready timing) + `writeQueryState` kind-preservation; mutual exclusivity with `search=`; Playwright deep-link coherence test; Codex code-review; open PR. (Codex guardrails are in commit `f2eac35`'s message + the plan in `/tmp/p248.md`.)
- [ ] **Close #245** (facet-padding) — superseded by #251 (RY hadn't confirmed; do at pickup).
- [ ] **#244** (collection-facet DRAFT) and **#246** (points-over-heatmap) — need rebase on the new `main` (A1 + facet-padding); #246 worth checking points-over-heatmap *under a search*.
- [ ] **#248 Flavor B** (arbitrary/Getty URIs) — needs URI→label resolution + free-text fallback; follow-up.
- [ ] **Testing-gap follow-up**: add a deep-link **interactivity** regression test (assert `enableInputs`/no-trackedEntity + camera actually moves), using the `__a1camera` hook. (Hook is uncommitted/local; re-add when building the test.)
- [ ] Deferred A1 items: selection revalidation on search change; BM25 substrate (#168–172).

---

## Next Session Entry Point

> Start here: continue **#248 Flavor A** on `feat/described-by-concept` (foundation `f2eac35` done). Next concrete step is `doDescribedBy` + extracting `runPidSetResults` from `doSearch`, then the `described-by=` URL plumbing + mutual-exclusivity, then test → Codex review → PR. Verify loop: `python3 dev_server.py --dir docs --port 8099` + `HEADLESS=1 node tests/playwright/a1-verify.mjs`.

---

## Session History

| Date | Trust | Summary |
|---|---|---|
| 2026-05-30/31 | external-content | Shakedown root-caused; A1 logjam = backgrounded-tab artifact; A1 perf + facet-padding fixed; Codex loop → dual approval; **A1 merged & deployed to isamples.org** (#251); #248 started (`feat/described-by-concept` foundation). |
| 2026-05-29 | external-content | (prior) A1 scoping + globe logjam framing (superseded — there was no logjam). |
