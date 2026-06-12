# SPIKE_RESULTS.md

Actual verification query outputs from the #272 Phase 2 ingestion spike.

*Run 2026-06-12 on local files. All numbers are real — from executed DuckDB queries against local parquet files.*

---

## Environment

| Item | Value |
|---|---|
| Python | `~/.pyenv/versions/myenv/bin/python` |
| DuckDB | 1.4.4 |
| Base wide | `~/Data/iSample/pqg_refining/isamples_202604_wide.parquet` (279 MB) |
| Eric's OC wide | `~/Data/iSample/pqg_refining/oc_isamples_pqg_wide_2026-06-09.parquet` (275 MB) |
| Spike output | `/tmp/ingest_272_output/isamples_202608_wide.parquet` (302 MB) |
| Script | `scripts/ingest_oc_records.py` |

> **Note on base:** the spike used `isamples_202604_wide` (locally available). Production ingestion must use `isamples_202606_wide` (the Phase 1 overlay output, currently on R2 only). This affects the rock facet count (V7) and the concept minting (2 concepts minted here vs 1 when using 202606 base — `otheranthropogenicmaterial` is already present in 202606). All other numbers are base-independent.

---

## Phase 0: Local file inventory

```
~/Data/iSample/pqg_refining/
  isamples_202604_wide.parquet          279 MB   Apr 23 2026 (our pre-overlay wide)
  oc_isamples_pqg_wide_2026-06-09.parquet  275 MB   Jun 10 2026 (Eric's fresh wide)
  oc_isamples_pqg_wide.parquet         275 MB   Dec 1 2025 (older Eric wide)
  oc_isamples_pqg.parquet              691 MB   Jun 9 2025 (Eric narrow)
  zenodo_wide_2026-01-09.parquet       278 MB   (Zenodo base wide)
```

No local copy of `isamples_202606_wide.parquet` found. Must download from R2 for production run.

---

## Gap characterization queries

### Q1: OC MSR count in our wide
```sql
SELECT COUNT(*) FROM read_parquet('isamples_202604_wide.parquet')
WHERE otype='MaterialSampleRecord' AND n='OPENCONTEXT';
```
**Result: 1,064,831**

### Q2: OC MSR count in Eric's wide
```sql
SELECT COUNT(*) FROM read_parquet('oc_isamples_pqg_wide_2026-06-09.parquet')
WHERE otype='MaterialSampleRecord';
```
**Result: 1,110,791**

### Q3: New pids (Eric's \ ours)
```sql
SELECT COUNT(*) FROM (
    SELECT pid FROM read_parquet('oc_...wide.parquet') WHERE otype='MaterialSampleRecord'
    EXCEPT
    SELECT pid FROM read_parquet('isamples_202604_wide.parquet')
    WHERE otype='MaterialSampleRecord' AND n='OPENCONTEXT'
);
```
**Result: 67,187**

### Q4: Deleted pids (ours \ Eric's)
```sql
SELECT COUNT(*) FROM (
    SELECT pid FROM read_parquet('isamples_202604_wide.parquet')
    WHERE otype='MaterialSampleRecord' AND n='OPENCONTEXT'
    EXCEPT
    SELECT pid FROM read_parquet('oc_...wide.parquet') WHERE otype='MaterialSampleRecord'
);
```
**Result: 21,227** — pids in frozen export but absent from Eric's fresh wide (de-published or restructured OC items). Policy: keep in our wide, no action.

### Q5: Material breakdown for new records

First-non-root material concept for 67,187 new MSRs:

| Count | Material URI |
|---|---|
| 22,236 | `material/1.0/biogenicnonorganicmaterial` |
| 19,072 | `material/1.0/otheranthropogenicmaterial` |
| 10,315 | `material/1.0/organicmaterial` |
| 7,766  | `material/1.0/rock` |
| 7,282  | NULL (root-only, no specific concept) |
| 466    | `material/1.0/mineral` |
| 48     | `material/1.0/anthropogenicmetal` |
| 2      | `material/1.0/mixedsoilsedimentrock` |

### Q6: Coordinates for new records
```sql
-- Direct geometry on MSR: 0/67,187
-- Direct latitude float: 0/67,187
-- Via graph path (MSR->SE->GeoCoordLoc): 67,187/67,187
```
**All 67,187 new records have coordinates via MSR → p__produced_by → SamplingEvent → p__sample_location → GeospatialCoordLocation.**

Coordinate range: lat −55.19 to 71.04, lon −164.0 to 159.9 (global, not just Jordan).

Sample (Lithic ID Jordan records confirmed at lat≈31.87, lon≈35.89):
```
ark:/28722/k27m0sf3s  Lithic ID: 4130  lat=31.867087  lon=35.889976
```

### Q7: Schema drift
| Dimension | Detail |
|---|---|
| Cols in ours only | `p__curation` (INTEGER[]), `p__related_resource` (BIGINT[]) |
| Cols in Eric's only | none |
| Type differences | `row_id`: ours=BIGINT, Eric's=INTEGER; `p__has_*` arrays: ours=BIGINT[], Eric's=INTEGER[] |
| `n` column | Eric's wide: NULL for all MSRs; ours: 'OPENCONTEXT' |
| geometry column | ours=BLOB (WKB); Eric's=GEOMETRY (DuckDB native, auto-decoded by spatial ext.) |

### Q8: Full entity subgraph
| Entity type | Count | In our wide (by pid)? |
|---|---|---|
| MaterialSampleRecord | 67,187 | 0 (all new pids) |
| SamplingEvent | 67,187 | 0 |
| GeospatialCoordLocation (from SE) | 10,316 | 0 |
| GeospatialCoordLocation (from SamplingSite) | 6,514 | 0 |
| Unique GeoCoordLoc total | 11,399 | 0 |
| SamplingSite | 6,514 | 0 |
| Agent | 24 | 0 |
| **Total new entity rows** | **152,311** | — |

### Q9: IdentifiedConcept analysis
14 distinct concept URIs referenced by new MSRs. Against 202604 base:
- 12 already present
- 2 missing: `otheranthropogenicmaterial`, `earthsurface`

Against 202606 base (production):
- 13 already present (Phase 1 minted `otheranthropogenicmaterial`)
- 1 missing: `earthsurface` (3,924 new MSRs use it for `p__has_context_category`)

### Q10: row_id ranges
| File | Min | Max | Total rows |
|---|---|---|---|
| Our 202604 wide | 1 | 20,729,358 | 20,729,358 |
| Eric's wide | 1 | 2,465,485 | 2,465,485 |
| Spike output | 1 | 20,881,671 | 20,881,671 |

New rows allocated: 20,729,359 to 20,881,671 (152,313 = 152,311 entities + 2 concepts).

---

## Spike execution log

```
[   0.1s] schema checks passed
[   0.2s] new pids: 67,187
[   0.3s] extracting entity subgraph for new pids...
[   0.7s] subgraph: msr=67,187 se=67,187 geo=11,399 site=6,514 agent=24
[   0.7s] src max_row_id=20,729,358
[   0.7s] id_map: 152,311 entries, new row_id range 20729359 to 20,881,669, collisions=0
[   0.8s] minting 2 new IdentifiedConcept rows: ['otheranthropogenicmaterial', 'earthsurface']
[   0.8s] minted_concepts=2
[   0.9s] coords: 67,187 pids with coords, 0 duplicate-coord pids
[   0.9s] remapping p__ arrays for new MSR rows...
[   1.5s] p__ remapping tables built
[   1.5s] running pre-write trust checks...
[   1.6s] trust checks passed
[   1.6s] expected output rows: 20,729,358 src + 152,311 new entities + 2 concepts = 20,881,671
[   1.6s] writing output...
[   8.9s] wrote /tmp/ingest_272_output/isamples_202608_wide.parquet
[   9.2s] post-write: rows=20,881,671  dup_rowids=0  dup_pids=0  oc_msrs=1,132,018  n_check=PASS
[   9.7s] manifest -> /tmp/.../isamples_202608_wide.parquet.manifest.json
[   9.7s] done
```

**Total wall time: ~10s.**

---

## Post-write verification

### V1: Row count by otype in spike output
| otype | Count |
|---|---|
| MaterialSampleRecord | 6,748,119 (+67,187 vs 202604) |
| SamplingEvent | 6,421,358 (+67,187) |
| GeospatialCoordLocation | 5,991,681 (+11,399) |
| MaterialSampleCuration | 720,254 (unchanged) |
| SampleRelation | 501,579 (unchanged) |
| SamplingSite | 392,674 (+6,514) |
| IdentifiedConcept | 55,895 (+2: otheranthropogenicmaterial + earthsurface) |
| Agent | 50,111 (+24) |

### V2: OC MSR count in output
```
n=SESAR          4,688,386
n=OPENCONTEXT    1,132,018   (was 1,064,831; +67,187)
n=GEOME            605,554
n=SMITHSONIAN      322,161
```

### V3: New MSRs have geometry
```
total_new_msr=67,187
has_geometry=67,187   (100%)
has_lat=67,187        (100%)
```

### V4: Coord round-trip check (geometry == lat/lon)
```
lat=-0.34581  geo_lat=-0.34581  (MATCH)
lon=-80.1658  geo_lon=-80.1658  (MATCH)
```

### V5: p__ reference integrity
```
Dangling p__produced_by refs in new MSRs: 0
```

### V6: Concept reference integrity
```
Unresolved concept refs in new MSRs: 0
```

### V7: Rock facet count
| Base | Existing OC rock | New rock | Total |
|---|---|---|---|
| 202604 (spike) | 1,956 (pre-overlay) | 7,766 | 9,722 |
| 202606 (expected production) | ~30,272 (overlay-corrected) | 7,766 | ~38,038 |

The 1,956 vs 30,272 discrepancy is entirely explained by the Phase 1 concept overlay that's in 202606 but not 202604. New record ingestion contributes exactly +7,766, matching the issue #272 comment exactly.

### V8: IdentifiedConcept count
```
Total IdentifiedConcept rows in output: 55,895
  202604 had: 55,893
  Phase 1 overlay added: +1 (otheranthropogenicmaterial, already in 202606)
  This spike added: +2 (both concepts, since base was 202604)
```

### V9: Output file size
```
Output: 301.5 MB
Src:    292.3 MB
Delta:  +9.2 MB (+3.1%)
```

### V10: Stage 4 derived files (build + validate)
```
build_frontend_derived.py:
  samp=6,748,119  samp_geo=6,047,469  duplicate_pids=0  duplicate_concept_row_ids=0
  sample_facets_v2 ✓  facet_summaries ✓  facet_cross_filter ✓
  samples_map_lite ✓  h3_summary_res{4,6,8} ✓
  Total time: ~6s

validate_frontend_derived.py: ALL CHECKS PASS (16/16)
  material root absent              PASS
  sentinel check                    PASS (expected anthropogenicmetal for 202604-base)
  facets pid unique                 PASS
  map_lite pid unique               PASS
  facets.pid == map_lite.pid        PASS (0 pids differ)
  facet_summaries algebraic         PASS
  cross_filter algebraic            PASS
  cross_filter baseline == summaries PASS
  h3 res4/6/8 sums match map_lite  PASS (6,047,469 each)
  facets schema contract            PASS
  facets non-empty                  PASS (6,047,469 rows >> 1,000,000)
  manifest sha256                   PASS
```

---

## Spike manifest (truncated)

```json
{
  "script": "ingest_oc_records.py",
  "duckdb_version": "1.4.4",
  "counts": {
    "src_rows": 20729358,
    "new_pids": 67187,
    "new_entity_rows": 152311,
    "minted_concepts": 2,
    "out_rows": 20881671,
    "new_oc_msr_total": 1132018,
    "entity_breakdown": {
      "new_msr": 67187, "new_se": 67187, "new_geo": 11399,
      "new_site": 6514, "new_agent": 24
    }
  },
  "output": {
    "bytes": 301517608,
    "sha256": "d8aad79b719ac61443462b5ddfcd350501b5ad2f77cfc435407435132d5ba52e"
  }
}
```

---

## Key bug fixed during spike

**Geometry type mismatch**: Eric's OC wide stores `geometry` as DuckDB `GEOMETRY` type (auto-decoded when the `spatial` extension is loaded). Our wide stores it as `BLOB` (WKB bytes). A naïve `UNION ALL BY NAME` between the src wide (BLOB) and rows with geometry from Eric's GeoCoordLoc (GEOMETRY) failed with:

```
ConversionException: Unimplemented type for cast (BLOB -> GEOMETRY)
```

**Fix**: Convert Eric's GEOMETRY → WKB BLOB using `ST_AsWKB(geometry)::BLOB` before the UNION. Validated: lat/lon round-trip identical to 6 decimal places.

This fix is in the script at two points:
1. `new_msr_coords` table: `ST_AsWKB(geo.geometry)::BLOB AS geometry`
2. `geo_select` (GeoCoordLoc rows): `ST_AsWKB(g.geometry)::BLOB AS geometry` (via `eric_geo_is_geometry=True` flag)

---

## Pipeline runtime summary

| Step | Time | Notes |
|---|---|---|
| `ingest_oc_records.py` (dry-run) | ~2s | Analysis + trust checks, no write |
| `ingest_oc_records.py` (full) | ~10s | Write 20.8M row parquet |
| `build_frontend_derived.py` | ~6s | All 6 derived files, no wide_h3 |
| `validate_frontend_derived.py` | ~3s | All 16 checks |
| **Total** | **~21s** | Full pipeline on 202604 base |
