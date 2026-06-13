# SYNC_RESULTS.md

Actual verification query outputs from the #272 Phase 2 production sync.

*Run 2026-06-12. All numbers are real — from executed DuckDB queries against local parquet files.*

---

## Environment

| Item | Value |
|---|---|
| Python | `~/.pyenv/versions/myenv/bin/python` (3.13.11) |
| DuckDB | 1.4.4 |
| Base wide (src) | `~/Data/iSample/pqg_refining/isamples_202606_wide.parquet` (292 MB) |
| Eric's OC wide | `~/Data/iSample/pqg_refining/oc_isamples_pqg_wide_2026-06-09.parquet` (288 MB) |
| Output wide | `/tmp/ingest_202608/isamples_202608_wide.parquet` (~302 MB) |
| Script | `scripts/ingest_oc_records.py` (production version with D3 removal) |

---

## Gap characterization against 202606 base

### Q1: OC MSR count in 202606 wide
```
SELECT COUNT(*) FROM ... WHERE otype='MaterialSampleRecord' AND n='OPENCONTEXT'
Result: 1,064,831
```

### Q2: OC MSR count in Eric's wide
```
SELECT COUNT(*) FROM ... WHERE otype='MaterialSampleRecord'
Result: 1,110,791
```

### Q3: New pids (Eric's \ 202606)
```
Result: 67,187
```

### Q4: Stale pids (202606 \ Eric's) — D3: REMOVED
```
Result: 21,227
```

### Q5: Expected OC MSR after sync
```
1,064,831 - 21,227 + 67,187 = 1,110,791
```
(Matches Eric's total exactly — true sync.)

### Q6: Total rows in 202606 wide
```
Result: 20,729,359
```
(One more than 202604's 20,729,358 — the extra row is `otheranthropogenicmaterial` concept minted by Phase 1.)

### Q7: max row_id in 202606
```
Result: 20,729,359
```

### Q8: Rock count in 202606 (OC MSRs)
```
Result: 30,272
```

### Q9: IdentifiedConcept count in 202606
```
Result: 55,894
```

### Q10: earthsurface in 202606
```
Result: 0 (not yet present — must be minted)
```

---

## Orphan analysis (D3 removal subgraph)

For the 21,227 removed MSR pids, orphan entities:

| Entity type | Count | Decision |
|---|---|---|
| MaterialSampleRecord | 21,227 | REMOVED |
| SamplingEvent (orphan — only in removed MSRs) | 21,227 | REMOVED |
| GeospatialCoordLocation (orphan — via orphan SEs) | 21,227 | REMOVED |
| SamplingSite (orphan — via orphan SEs) | 928 | REMOVED |
| Agent (orphan) | 0 | N/A (none) |
| **Total rows removed** | **64,609** | — |

Shared SamplingEvent rows (referenced by both removed + surviving MSRs): 0.

---

## Production run execution log

```
[   0.1s] schema checks passed
[   0.2s] stale pids to remove: 21,227
[  14.1s] orphan subgraph: msr=21,227 se=21,227 geo=21,227 site=928 total=64,609
[  14.1s] rows_to_remove: 64,609 (matches orphan arithmetic)
[  14.1s] new pids: 67,187
[  14.1s] extracting entity subgraph for new pids...
[  14.5s] subgraph: msr=67,187 se=67,187 geo=11,399 site=6,514 agent=24
[  14.5s] src max_row_id=20,729,359
[  14.6s] id_map: 152,311 entries, new row_id range 20729360 to 20,881,670, collisions=0
[  14.6s] minting 1 new IdentifiedConcept rows: ['https://w3id.org/isample/vocabulary/sampledfeature/1.0/earthsurface']
[  14.6s] minted_concepts=1
[  14.7s] coords: 67,187 pids with coords, 0 duplicate-coord pids
[  14.7s] remapping p__ arrays for new MSR rows...
[  15.3s] p__ remapping tables built
[  15.3s] running pre-write trust checks...
[  15.4s] trust checks passed
[  15.4s] expected output rows: 20,729,359 src - 64,609 removed + 152,311 new entities + 1 concepts = 20,817,062
[  15.4s] writing output...
[  22.4s] wrote /tmp/ingest_202608/isamples_202608_wide.parquet
[  22.6s] post-write: rows=20,817,062  dup_rowids=0  dup_pids=0  oc_msrs=1,110,791  stale_remain=0  n_check=PASS
[  23.1s] manifest -> /tmp/ingest_202608/isamples_202608_wide.parquet.manifest.json
[  23.1s] done
```

**Total ingest time: ~23s.**

---

## Post-write verification queries (all run against 202608 wide + derived files)

### Va: Row count by otype in output
| otype | Count | Delta vs 202606 |
|---|---|---|
| MaterialSampleRecord | 6,726,892 | +67,187 new − 21,227 removed = +45,960 |
| SamplingEvent | 6,400,131 | +67,187 new − 21,227 orphan = +45,960 |
| GeospatialCoordLocation | 5,970,454 | +11,399 new − 21,227 orphan = −9,828 |
| MaterialSampleCuration | 720,254 | unchanged |
| SampleRelation | 501,579 | unchanged |
| SamplingSite | 391,746 | +6,514 new − 928 orphan = +5,586 |
| IdentifiedConcept | 55,895 | +1 (earthsurface minted) |
| Agent | 50,111 | +24 new, 0 orphan |
| **Total** | **20,817,062** | **expected** |

### Vb: OC MSR count
```
n=OPENCONTEXT: 1,110,791   (expected: 1,110,791) ✓
```

### Vc: OC rock facet count (from sample_facets_v2)
```
OC rock count: 37,953
Arithmetic: 30,272 (202606) - 85 (removed rocks) + 7,766 (new rocks) = 37,953 ✓
```

### Vd: Removed Murlo pids — NONE in output
```
Stale pids remaining in output: 0 (expected: 0) ✓
```

### Ve: New r2p24 Murlo-style pids — ALL present
```
r2p24 pids in Eric's wide: 15,409
r2p24 pids in output:      15,409 ✓
```
(814 of the 15,409 r2p24 pids are newly added; the remainder were already in the 202606 wide from earlier Murlo work.)

### Vf: Jordan lithic spot-checks (Tall al-ʿUmayri, new rock records)
```
ark:/28722/k27m0sf3s  lat=31.867088  lon=35.889977 ✓
ark:/28722/k27w6wk0d  lat=31.868178  lon=35.888423 ✓
ark:/28722/k2gq7df90  lat=31.868287  lon=35.888424 ✓
```
Jordan lithic new pids (lat 31–32.5, lon 35–36.5): 7,925.

### Vg: earthsurface concept
```
IdentifiedConcept rows with pid containing 'earthsurface': 1 (expected: 1) ✓
```

### Vh: IdentifiedConcept total
```
55,895  (202606 had 55,894, +1 for earthsurface) ✓
```

### Vi: Stage 4 derived files
```
build_frontend_derived.py:
  samp=6,726,892  samp_geo=6,026,242  duplicate_pids=0  duplicate_concept_row_ids=0
  sample_facets_v2 ✓  facet_summaries ✓  facet_cross_filter ✓
  samples_map_lite ✓  h3_summary_res{4,6,8} ✓
  Total time: ~6s
```

### Vj: Stage 4 validator (ALL 25 CHECKS PASS)
```
validate_frontend_derived.py --dir /tmp/ingest_202608/ --tag isamples_202608 --wide ...

ALL CHECKS PASS (25/25)

Selected checks:
  material root absent              PASS
  sentinel check                    PASS (otheranthropogenicmaterial — post-#272 value)
  facets pid unique                 PASS
  map_lite pid unique               PASS
  facets.pid == map_lite.pid        PASS (0 pids differ)
  facet_summaries algebraic         PASS
  cross_filter algebraic            PASS
  cross_filter baseline == summaries PASS
  h3 res4/6/8 sums match map_lite  PASS (6,026,242 each)
  facets schema contract            PASS
  facets non-empty                  PASS (6,026,242 rows)
  facets == fresh build from --wide PASS
  map_lite == fresh build from --wide PASS
  h3 discrete == fresh build (res4/6/8) PASS
  h3 centers within 1e-5 (res4/6/8) PASS
  manifest sha256                   PASS
```

---

## Fixture tests (20 passed)
```
pytest tests/test_ingest_oc_records.py -v

20/20 passed in 4.47s
```
Tests cover: new pid ingestion, stale pid removal, orphan subgraph removal, non-OC row survival,
geometry denormalization, n='OPENCONTEXT' assignment, p__ array remapping, concept minting,
SamplingSite ingestion, row count arithmetic, no duplicate row_ids, no duplicate MSR pids,
determinism, dry-run, hard-fail on duplicate OC pids, hard-fail on collision, removal scope,
id collision avoidance, overwrite guard.

---

## Pipeline runtime summary

| Step | Time | Notes |
|---|---|---|
| Download 202606 wide | ~25s | 292 MB from R2 |
| `ingest_oc_records.py` (dry-run) | ~15s | Analysis + trust checks (includes orphan analysis) |
| `ingest_oc_records.py` (full) | ~23s | Write 20.8M-row parquet |
| `build_frontend_derived.py` | ~6s | All 6 derived files, no wide_h3 |
| `validate_frontend_derived.py` (full, incl. --wide) | varies | 25 checks |
| **Total** | **~60s** | Full pipeline on 202606 base |

---

## Input file checksums

| File | sha256 |
|---|---|
| isamples_202606_wide.parquet | 57c01f922c52bac2c6a28abd504f38161f83c140a7149036b3d8e725be8aa3b1 |
| oc_isamples_pqg_wide_2026-06-09.parquet | (see manifest) |
| isamples_202608_wide.parquet | (see /tmp/ingest_202608/isamples_202608_wide.parquet.manifest.json) |
