# DESIGN_272_INGEST.md

Design document for ingesting 67,187 new OpenContext records into the iSamples derived-data pipeline (GitHub issue #272, follow-up phase).

*All numbers in this document are from executed DuckDB queries against local files. See SPIKE_RESULTS.md for raw output.*

*Prepared by rbotyee (Claude Code spike), 2026-06-12. Decisions resolved 2026-06-12 (see Section 7). See SYNC_RESULTS.md for actual run output.*

---

## 1. Background and context

Issue #272 Phase 1 (PR #275, merged and live) overlaid corrected material/object-type concept mappings from Eric's fresh OC PQG onto the 1,043,604 OC pids already in the unified wide. That phase deliberately skipped new-record ingestion: **67,187 OC records present in Eric's 2026-06-09 wide are absent from the production `isamples_202606_wide.parquet`.**

This document designs Phase 2: ingest those 67,187 records.

### Key files

| File | Path |
|---|---|
| Production base wide (Phase 1 output) | `https://data.isamples.org/isamples_202606_wide.parquet` |
| Eric's fresh OC wide | `~/Data/iSample/pqg_refining/oc_isamples_pqg_wide_2026-06-09.parquet` |
| Pre-Phase-1 wide (local copy) | `~/Data/iSample/pqg_refining/isamples_202604_wide.parquet` |

> **Note:** The spike ran against the 202604 wide (locally available) rather than 202606 (remote only). For the two dimensions that differ — row_id range and IdentifiedConcept inventory — the spike accounts for the 202606 delta explicitly.

---

## 2. Gap characterization (all from executed queries)

### 2.1 Record counts

| Measure | Count |
|---|---|
| OC MaterialSampleRecords in our 202604 wide | 1,064,831 |
| OC MaterialSampleRecords in Eric's fresh wide | 1,110,791 |
| **New pids (Eric's \ ours)** | **67,187** |
| Deleted pids (ours \ Eric's, not in Eric's at all) | 21,227 |

**D3 DECISION (2026-06-12, RY):** These 21,227 stale pids ARE REMOVED. OpenContext mass-updated Murlo project PIDs; old PIDs would duplicate the same physical samples. This is a TRUE SYNC: add new + remove stale in one operation. The orphaned subgraph entities (SamplingEvent, GeoCoordLoc, SamplingSite) linked ONLY by removed MSRs are also removed. Agents are NOT removed (0 orphan agents found). See Section 3.5 for orphan analysis results.

### 2.2 Material type breakdown of new records

First-non-root material per new MSR (Eric's OC concept arrays):

| Material URI | Count |
|---|---|
| `material/1.0/biogenicnonorganicmaterial` | 22,236 |
| `material/1.0/otheranthropogenicmaterial` | 19,072 |
| `material/1.0/organicmaterial` | 10,315 |
| `material/1.0/rock` | 7,766 |
| NULL (root-only, no specific concept) | 7,282 |
| `material/1.0/mineral` | 466 |
| `material/1.0/anthropogenicmetal` | 48 |
| `material/1.0/mixedsoilsedimentrock` | 2 |

The 7,766 rock records are the Tall al-ʿUmayri Jordan lithics flagged by Eric during staging inspection (7,903 labelled "Lithic ID: …", of which 7,766 are rock-first-non-root).

### 2.3 Geographic coverage

All 67,187 new MSRs have **no geometry blob and no latitude/longitude directly on the MSR row** in Eric's wide. However:

- 67,187 / 67,187 (100%) have coordinates accessible via the graph: `MSR → p__produced_by → SamplingEvent → p__sample_location → GeospatialCoordLocation.{geometry, latitude, longitude}`
- Coordinate range: lat −55.19 to 71.04, lon −164.0 to 159.9 (global coverage, not just Jordan)
- 0 duplicates in the coord path (each MSR maps to exactly 1 coordinate row)

The builder (`build_frontend_derived.py`) reads `geometry` from the wide's MSR rows using `ST_X(geometry)`/`ST_Y(geometry)`. The ingestion step must **denormalize** the GeoCoordLoc geometry blob onto each new MSR row.

### 2.4 New entity subgraph

The 67,187 new MSRs bring a full entity subgraph that must be ingested together:

| Entity type | Count | Already in our wide? |
|---|---|---|
| MaterialSampleRecord | 67,187 | 0 (new pids) |
| SamplingEvent | 67,187 | 0 (all new PIDs absent) |
| GeospatialCoordLocation (from SE) | 10,316 | 0 |
| GeospatialCoordLocation (from SamplingSite) | 6,514 | 0 |
| **Unique GeoCoordLoc total** | **11,399** | **0** |
| SamplingSite | 6,514 | 0 |
| Agent | 24 | 0 |
| **Total new entity rows** | **~152,312** | — |

No entity (by PID) from the new subgraph already exists in the 202604 wide. The 202606 wide is identical in entity inventory (it only changed p__ columns on existing rows + minted 1 concept), so this holds for 202606 too.

### 2.5 IdentifiedConcept inventory

New MSRs reference 14 distinct concept row_ids in Eric's wide. Of these:

| Concept URI | In 202604? | In 202606? |
|---|---|---|
| `material/1.0/anthropogenicmetal` | YES | YES |
| `material/1.0/biogenicnonorganicmaterial` | YES | YES |
| `material/1.0/material` (root) | YES | YES |
| `material/1.0/mineral` | YES | YES |
| `material/1.0/mixedsoilsedimentrock` | YES | YES |
| `material/1.0/organicmaterial` | YES | YES |
| `material/1.0/otheranthropogenicmaterial` | NO | **YES** (minted by Phase 1) |
| `material/1.0/rock` | YES | YES |
| `materialsampleobjecttype/1.0/artifact` | YES | YES |
| `materialsampleobjecttype/1.0/biologicalmaterialsample` | YES | YES |
| `materialsampleobjecttype/1.0/materialsample` | YES | YES |
| `materialsampleobjecttype/1.0/organismpart` | YES | YES |
| `sampledfeature/1.0/earthsurface` | NO | NO (need to mint) |
| `sampledfeature/1.0/pasthumanoccupationsite` | YES | YES |

**Conclusion:** Using 202606 as the base, exactly **1 new concept must be minted**: `sampledfeature/1.0/earthsurface` (used by 3,924 of the new MSRs for `p__has_context_category`).

### 2.6 Schema drift between our wide and Eric's wide

| Dimension | Detail |
|---|---|
| Columns in ours NOT in Eric's | `p__curation` (INTEGER[]), `p__related_resource` (BIGINT[]) |
| Columns in Eric's NOT in ours | none |
| Type differences | `row_id`: ours=BIGINT, Eric's=INTEGER; `p__has_*` arrays: ours=BIGINT[], Eric's=INTEGER[] |
| `n` (source) column | Eric's wide has NULL for all MSRs; ours uses 'OPENCONTEXT' |

The two extra columns (`p__curation`, `p__related_resource`) are NULL for OC records in the existing wide (the frozen export never populated them for OC). New OC records will also have NULLs for these columns — that is the correct behavior.

The `n` column mismatch is critical: **ingested rows must have `n = 'OPENCONTEXT'` set explicitly**.

---

## 3. Row-id allocation design

### 3.1 Constraint

Our wide has `row_id BIGINT` ranging 1–20,729,358. Eric's wide has `row_id INTEGER` ranging 1–2,465,485. These ranges **overlap** — we cannot reuse Eric's row_ids.

### 3.2 Strategy: dense rank starting at `max_src + 1`

All new rows get row_ids assigned by dense rank starting at `max(src.row_id) + 1 = 20,729,359`. The rank ordering must be deterministic (by a stable sort key) to make the output reproducible. Proposed ordering: by `(otype, pid)` — otype first groups entity classes together, pid within each class is stable.

Revised row count:
- New rows: ~152,312 (plus concept minting: +1)
- New row_id range: 20,729,359 to ~20,881,671

**This leaves substantial headroom** and avoids all collisions.

### 3.3 p__ array rewriting

Eric's wide stores `p__produced_by`, `p__sample_location`, etc. as INTEGER[] references into Eric's row_id space. All these references must be **remapped to the new row_ids** in our id space. This requires:

1. Build a mapping table: Eric's `row_id` → new `row_id` for every entity in the new subgraph.
2. Apply the mapping to all p__ columns on new MSR rows.

**This is the most complex step** — see Section 4.3.

---

## 4. Pipeline design: `make ingest-272`

### 4.1 Overview

```
202606_wide.parquet  +  oc_isamples_pqg_wide_2026-06-09.parquet
    │
    ▼ scripts/ingest_oc_records.py
    │   Phase A: Identify new pids (Eric's \ ours)
    │   Phase B: Extract full entity subgraph for new pids
    │   Phase C: Assign new row_ids (dense rank, deterministic)
    │   Phase D: Remap p__ arrays from Eric's id space to our id space
    │   Phase E: Denormalize coords (geometry BLOB) onto new MSR rows
    │   Phase F: Set n='OPENCONTEXT' on new MSR rows
    │   Phase G: Mint new IdentifiedConcept rows (earthsurface)
    │   Phase H: Hard-fail checks (dup row_ids, dup pids, all refs resolved)
    │   Phase I: Write  src_rows UNION ALL new_rows → isamples_202608_wide.parquet
    │
    ▼ scripts/validate_oc_concept_enrichment.py (reused for concept rows)
    │   (or a new validate_ingest.py — see Section 6)
    │
    ▼ scripts/build_frontend_derived.py
    │
    ▼ scripts/validate_frontend_derived.py --wide isamples_202608_wide.parquet
```

### 4.2 Tag convention

The output should be tagged `isamples_202608` (next month tag) to reflect the new data vintage. RY decision gate: confirm tag before publishing.

### 4.3 p__ array remapping in detail

This is the core correctness challenge. Each new entity row in Eric's wide has a `row_id` in Eric's space (1–2,465,485). Each p__ column on a new MSR row is an `INTEGER[]` of those Eric row_ids. After ingestion, those integers must refer to our new row_ids (20,729,359+).

Algorithm:

```sql
-- Step 1: collect all Eric row_ids that need remapping
CREATE TEMP TABLE eric_id_map AS
  SELECT eric_row_id,
         max_src_row_id + DENSE_RANK() OVER (ORDER BY otype, pid) AS our_row_id
  FROM all_new_entities;   -- new MSR + SE + Geo + Site + Agent + Concept

-- Step 2: remap p__ arrays on new MSR rows
-- Use list_transform or UNNEST+re-aggregate pattern
SELECT
  ...,
  (SELECT list(m.our_row_id ORDER BY u.ord) 
   FROM UNNEST(e.p__produced_by) WITH ORDINALITY AS u(rid, ord)
   JOIN eric_id_map m ON m.eric_row_id = u.rid) AS p__produced_by,
  ...
FROM new_msr_rows e
```

> **Warning:** the UNNEST+correlated subquery approach must be avoided at 67K rows (we learned the MAP cross-join blowup lesson). Use the decorrelated pattern from `build_frontend_derived.py` (UNNEST WITH ORDINALITY + JOIN + arg_min/list-agg) or a pre-aggregated mapping table.

### 4.4 Geometry denormalization

The builder reads `geometry` from MSR rows. New MSR rows have no geometry. We must lift the geometry blob from the linked `GeospatialCoordLocation`:

```sql
-- In the new MSR rows:
-- MSR.p__produced_by[1] -> SamplingEvent.row_id
-- SamplingEvent.p__sample_location[1] -> GeoCoordLoc.row_id
-- GeoCoordLoc.geometry -> copy to MSR.geometry
-- GeoCoordLoc.latitude, .longitude -> copy to MSR.latitude, .longitude

CREATE TEMP TABLE new_msr_coords AS
  WITH msr_se AS (
    SELECT m.pid, se.p__sample_location
    FROM new_msr_eric m, UNNEST(m.p__produced_by) AS u(se_rid)
    JOIN new_se_eric se ON se.row_id = u.se_rid
  )
  SELECT ms.pid, geo.geometry, geo.latitude, geo.longitude
  FROM msr_se ms, UNNEST(ms.p__sample_location) AS u(geo_rid)
  JOIN new_geo_eric geo ON geo.row_id = u.geo_rid;
```

Confirmed: 0 duplicate coord rows per pid; 67,187 / 67,187 (100%) have coords.

### 4.5 Hard-fail invariants (trust gate)

The script must refuse to write if any of these hold:

1. **Duplicate new pids**: any pid in new MSR rows already in src wide → wrong grain
2. **Duplicate row_ids**: any new row_id in the output overlaps an existing row_id
3. **Unresolved p__ references**: any p__ array element in any new row points to a row_id that doesn't exist in the output
4. **Missing n column**: any new MSR row has `n IS NULL` (must be 'OPENCONTEXT')
5. **Missing geometry on placed MSR**: any new MSR with a coord path but null geometry in output
6. **Row count**: output must equal `src_rows + new_entity_rows + minted_concepts`

### 4.6 Makefile target

```makefile
# make ingest-272 TAG=isamples_202608
ingest-272: $(OC_WIDE) 
    $(PY) scripts/ingest_oc_records.py \
        --src $(ENRICHED) \          # isamples_202606_wide.parquet
        --oc-wide $(OC_WIDE) \       # oc_isamples_pqg_wide_2026-06-09.parquet
        --out $(OUTDIR)/$(TAG)_wide.parquet
    $(MAKE) derived DERIVED_WIDE=$(OUTDIR)/$(TAG)_wide.parquet TAG=$(TAG)
    $(MAKE) validate TAG=$(TAG) SENTINEL_FLAG=
```

> **RY decision gate:** Should `ingest-272` stack on `all-272` (requiring 202606 wide as input) or should it operate independently against the R2 202606 URL? Stacking is cleaner but requires the local 202606 file.

---

## 5. Schema mapping: Eric's wide columns → our wide columns

All 47 shared columns are taken directly from Eric's wide with the following transformations:

| Column | Treatment |
|---|---|
| `row_id` | Replaced by new id from `eric_id_map` (see §4.3) |
| `n` | Set to `'OPENCONTEXT'` (Eric's wide has NULL for all MSR rows) |
| `p__produced_by`, `p__sample_location`, `p__sampling_site`, `p__site_location`, `p__registrant` | Remapped via `eric_id_map` (INTEGER[] → BIGINT[]) |
| `p__has_material_category`, `p__has_sample_object_type`, `p__has_context_category` | Remapped via `eric_id_map` + `our_concept_map` (URI lookup for concepts) |
| `p__keywords` | Remapped if non-null (INTEGER[] → BIGINT[]) |
| `p__responsibility` | Remapped if non-null (INTEGER[] → BIGINT[]) |
| `geometry` | Lifted from linked GeoCoordLoc row (WKB BLOB) |
| `latitude`, `longitude` | Lifted from linked GeoCoordLoc row |
| `p__curation` | NULL (INTEGER[] — not in Eric's wide, not applicable to OC) |
| `p__related_resource` | NULL (BIGINT[] — not in Eric's wide) |

No column present in Eric's wide is dropped (47 columns in, 49 in ours = 47 + 2 nulled columns).

---

## 6. Validation design

The existing `validate_frontend_derived.py` covers Stage 4 (derived files). We need an additional trust gate for the new Stage 3c (ingest):

**`scripts/validate_ingest.py`** (new, to be written with the impl):
- Re-derive the new-pid set from inputs (Eric's wide \ src wide) and assert the output contains exactly those pids
- Assert row count: `output_rows == src_rows + new_entity_rows + minted_concepts`
- Assert no duplicate row_ids in output
- Assert all p__ references in new rows resolve to existing rows in output
- Assert new MSR rows have `n = 'OPENCONTEXT'`
- Assert geometry non-null for all new MSRs that had a coord path
- Assert concept count: `output IdentifiedConcept count == src concept count + minted count`

The Stage 4 semantic gate (`validate_frontend_derived.py --wide`) should be run unchanged on the ingested wide — it doesn't know about Phase 2 vs Phase 1, it just validates the derived files against the wide.

---

## 3.5 Orphan analysis (actuals from 202606 base)

For the 21,227 removed MSR pids, orphan entities (referenced ONLY by removed MSRs):

| Entity type | Orphan count | Policy |
|---|---|---|
| MaterialSampleRecord | 21,227 | REMOVE (the removed pids themselves) |
| SamplingEvent | 21,227 | REMOVE (each removed MSR had exactly 1 orphan SE) |
| GeospatialCoordLocation | 21,227 | REMOVE (each orphan SE had 1 orphan geo via p__sample_location) |
| SamplingSite | 928 | REMOVE (orphan sites via orphan SE p__sampling_site) |
| Agent | 0 | KEEP (no orphan agents — all shared with surviving MSRs) |
| **Total rows removed** | **64,609** | — |

No SamplingSite geo refs (p__site_location) produced additional orphan geo rows (those 928 sites' geo rows were shared with surviving entities or already counted).

---

## 7. Decisions (all resolved 2026-06-12)

| # | Decision | **Resolution** |
|---|---|---|
| D1 | **Base wide for ingestion** | **202606** — downloaded from R2; sha256=57c01f922c52bac2c6a28abd504f38161f83c140a7149036b3d8e725be8aa3b1 |
| D2 | **Output tag** | **isamples_202608** |
| D3 | **Stale pids policy** | **REMOVE** — TRUE SYNC. 21,227 stale OC pids removed + 43,382 orphan subgraph entities. Rationale: Murlo project mass-PID-update; old pids would duplicate physical samples. |
| D4 | **n column for non-MSR entities** | **NULL** — matches existing convention; only MSR rows get n='OPENCONTEXT' |
| D5 | **p__curation / p__related_resource** | **NULL** — no source in Eric's PQG; matches existing OC rows |
| D6 | **Staging vs production** | **Stage first** — output at /tmp/ingest_202608/; R2 publish human-gated |
| D7 | **Eric notification** | OC rock count after sync: **37,953** (30,272 - 85 removed rocks + 7,766 new rocks). Eric to verify. |

---

## 8. Gap resolution (as of 2026-06-12 implementation)

1. ✅ **202606 round-trip**: DONE. Production run used 202606 as base. Confirmed: 1 concept minted (earthsurface), not 2.

2. ✅ **p__ array remapping correctness at scale**: DONE. Full remapping executed; post-write trust checks + 25-check validator all pass.

3. **SamplingSite deduplication**: NOT resolved (acknowledged data-quality gap; not a correctness issue for ingestion).

4. ✅ **Keywords entities**: p__keywords concept resolution is included in the remap tables (remap_msr_kw). All keyword concept refs in new MSRs resolve via URI lookup against src IdentifiedConcept rows.

5. **Agent deduplication**: NOT resolved (acknowledged; 24 new agents are conceptually distinct by pid).

6. ✅ **Geometry WKB encoding**: Confirmed. Eric's geo uses GEOMETRY type; ingest converts with ST_AsWKB()::BLOB. Coord round-trip accurate to 6 decimal places (validated by build + validator).

7. ✅ **File sizes measured**: 202608 wide is ~302MB (202606 was ~292MB, +3.4%). samp=6,726,892, samp_geo=6,026,242.

8. ✅ **Fixture tests**: `tests/test_ingest_oc_records.py` added with 20 tests covering all trust-gate invariants, remapping, removal, and determinism.

---

## 9. Summary

The ingestion is well-understood and low-risk:
- 67,187 new MSRs + ~85,125 supporting entities = ~152,312 new rows
- All new entities are absent from the current wide (no pid collisions)
- All coords accessible via graph path (100% coverage)
- 1 new concept to mint (earthsurface, used by 3,924 new MSRs)
- Primary complexity: row_id remapping from Eric's space to ours
- The Phase 1 overlay pattern (`enrich_wide_with_oc_concepts.py`) is the precedent for the write step
- The full Stage 4 semantic gate runs unchanged on the output

Estimated implementation effort: 1–2 sessions (script + tests + gate).

---

## 10. Phase 3 — Bug fixes folded into 202608 (2026-06-12)

Three bugs discovered during triage are fixed before staging 202608. All fixes are folded into the 202608 rebuild so the published dataset is clean from the start.

### Fix A — #277: OC description enrichment

**Root cause**: The 202608 (and previous 202606) combined wide stores OC sample `description` as terse Linked Data metadata (`'updated': 2023-10-05T04:45:54Z`) instead of the human-readable site-path strings (`Open Context published "Sample" from: Europe/Cyprus/PKAP Survey Area/...`) present in Eric's OC PQG wide.

**Impact**: Text search for "Cyprus" returns 0 matches in the deployed explorer (expected ≈ 69,230).

**Fix**: Added **Phase J** to `scripts/ingest_oc_records.py`. After the sync write, the script reads the output wide, LEFT JOINs on `pid` with Eric's OC wide for OC MSR rows only, overwrites `description` with Eric's value if non-null, and atomically replaces the output. Non-OC rows and rows with NULL description in Eric's wide are unchanged.

**Trust gate**: Cyprus OC MSR count after enrichment must be ≈ 69,230.

**Schema note**: `description` is a simple VARCHAR in both wides. Row counts are invariant.

### Fix B — #283a: Empty-string facet filter

**Root cause**: 586 GEOME records have an empty string (`''`) as their `context` (Sampled Feature) facet value because their `p__has_context_category` points to an `IdentifiedConcept` row with `pid = ''`. The facet-summary builder filtered `IS NOT NULL` but not empty strings, so `''` appeared as a blank selectable facet entry with count 586.

**Fix**: Changed the WHERE filter in `build_facet_summaries` and `build_facet_cross_filter` in `scripts/build_frontend_derived.py` from `IS NOT NULL` to `IS NOT NULL AND {d} <> ''`. Also updated the algebraic recompute in `scripts/validate_frontend_derived.py` to match, and added check 5b (`facet_summaries no blank values (#283a)`).

**Trust gate**: `SELECT COUNT(*) FROM facet_summaries WHERE facet_value = ''` must be 0.

### Fix C — #283b: Deprecated specimentype/1.0 label mappings

**Root cause**: 169 SESAR records use deprecated-namespace object_type URIs (`specimentype/1.0/othersolidobject`, `specimentype/1.0/physicalspecimen`) that are absent from `vocab_labels.parquet`. The explorer's `prettyLabel()` falls back to displaying the raw URI path tail.

**Fix**: Added `MANUAL_LABEL_OVERRIDES` to `scripts/build_vocab_labels.py` — two hardcoded rows injected before the dedupe step. Rebuilt `vocab_labels.parquet` (now 539 rows, up from 537).

**Trust gate**: Both URIs present in `vocab_labels.parquet` with correct `pref_label` values.

### Fixture tests added (tests/test_ingest_oc_records.py)

| Group | Tests added |
|---|---|
| Fix #277 | `test_oc_description_enriched_from_eric_wide`, `test_non_oc_description_unchanged_by_enrichment`, `test_oc_msr_count_unchanged_by_enrichment` |
| Fix #283a | `test_empty_string_facet_values_filtered_from_summaries`, `test_empty_string_facet_values_filtered_from_cross_filter` |
| Fix #283b | `test_specimentype_othersolidobject_in_vocab_labels`, `test_specimentype_physicalspecimen_in_vocab_labels`, `test_specimentype_labels_have_lang_en` |

All 28 tests pass (`pytest tests/test_ingest_oc_records.py -v`).

---

## Phase 4 — Codex Blocker Fixes (2026-06-13)

A Codex pre-merge review of the Phase 3 code found 3 latent bugs (blockers) and 4 nits. The STAGED DATA in /tmp/ingest_202608/ was verified correct (zero dangling refs), but the CODE contained landmines that would silently corrupt a future re-run. All bugs are fixed and the pipeline was rebuilt from scratch.

### Blocker 1: Cross-source orphan protection

**File**: `scripts/ingest_oc_records.py`, lines ~213-216 (surviving_se_refs query)

**Bug**: The `surviving_se_refs`, `surviving_geo_refs`, and `surviving_site_refs` queries were filtered to `s.n='OPENCONTEXT'`, meaning a SamplingEvent/Geo/Site referenced by a surviving SESAR/GEOME/Smithsonian MSR was NOT protected and could be wrongly deleted as an orphan.

**Fix**: Removed the `n='OPENCONTEXT'` filter from `surviving_se_refs`. The surviving refs now come from ALL surviving MaterialSampleRecords regardless of source. The condition `NOT (s.n='OPENCONTEXT' AND s.pid IN removed_pids)` correctly covers both OC survivors and all non-OC MSRs.

**Impact**: In production, shared SEs/Geos/Sites (referenced by both OC and non-OC MSRs) would have been incorrectly deleted, creating dangling refs in SESAR/GEOME rows. The actual 202608 data was unaffected (the production run had 0 shared SEs between OC-removed and non-OC MSRs), but the latent bug was critical.

**Regression test**: `test_cross_source_shared_entity_not_orphaned` — confirmed FAILS on old code and PASSES on fixed code.

### Blocker 2: Incomplete reference extraction + trust gate

**File**: `scripts/ingest_oc_records.py`

**Bug A** (line ~373): Agent extraction only queried `p__registrant`. Agents in `p__responsibility` were remapped into `eric_id_map` but their Agent entity rows were never extracted — meaning `p__responsibility` references on new MSR rows pointed to row_ids that didn't exist in the output (dangling refs).

**Fix A**: Extended `agent_ids` UNION to also include `p__responsibility`:
```sql
UNION
SELECT DISTINCT u.agent_id FROM new_msr_eric, UNNEST(p__responsibility) AS u(agent_id)
```

**Bug B** (line ~648): The pre-write trust gate only checked `p__produced_by` and 3 concept dims. Other p__* columns (p__registrant, p__responsibility, p__sample_location, etc.) were not verified.

**Fix B**: Added a comprehensive post-write dangling-ref trust gate (B2B) that checks EVERY p__* array column (BIGINT[] and INTEGER[]) on new rows against the output row_id set. HARD FAIL (RuntimeError) if any dangling ref is found. Covers 10 columns total.

### Blocker 3: Non-deterministic output order (Phase J)

**File**: `scripts/ingest_oc_records.py`, Phase J UNION ALL (~line 966)

**Bug**: Phase J rewrites the output with a UNION ALL (OC MSR rows + non-OC rows) but lacked `ORDER BY row_id`. The initial Phase I write had `ORDER BY row_id`, but Phase J's rewrite lost it — making sha256 non-reproducible across runs.

**Fix**: Added `ORDER BY row_id` to the Phase J COPY query before the final `TO ... PARQUET` write.

### Nit A: Whitespace-only facet values

**Files**: `scripts/build_frontend_derived.py`, `scripts/validate_frontend_derived.py`

Changed `{d} <> ''` to `NULLIF(TRIM({d}), '') IS NOT NULL` in both `build_facet_summaries` and `build_facet_cross_filter`. Updated validator check 5b to use `TRIM(facet_value) = ''` to catch whitespace-only values.

### Nit B: Cyprus enrichment as hard trust gate

**File**: `scripts/ingest_oc_records.py`

Changed the Cyprus description count from a log statement to a hard `RuntimeError` when `n_cyprus < 69,000` (at production scale, i.e., when `out_oc_count > 1,000,000`). Synthetic fixtures skip the gate to avoid false positives.

### Nit C: B1 regression test

**File**: `tests/test_ingest_oc_records.py`

Added `test_cross_source_shared_entity_not_orphaned`: synthetic fixture with OC MSR to-be-removed + SESAR MSR sharing SE/Site/Geo. Confirmed test FAILS on old code, PASSES on fixed code.

### Fixture tests (29 total after Phase 4)

```
pytest tests/test_ingest_oc_records.py -v
29/29 passed in ~11s

New test added (Phase 4):
  Blocker 1: test_cross_source_shared_entity_not_orphaned
```
