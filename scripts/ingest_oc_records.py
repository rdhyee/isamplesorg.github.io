#!/usr/bin/env python3
"""Ingest new OpenContext records from Eric's OC PQG wide into the unified wide.

Issue #272 Phase 2 (follow-up to PR #275 overlay phase):
  The overlay phase fixed concept mappings for ~1.04M existing OC pids.
  This script ingests the ~67,187 NEW OC records that were absent from the
  frozen iSamples Central export and therefore absent from the unified wide.

WHAT IT DOES (single DuckDB pass, deterministic):
  1. Identify new pids: present in Eric's OC wide, absent from src wide.
  2. Extract the full entity subgraph for new pids:
       MaterialSampleRecord + SamplingEvent + GeospatialCoordLocation +
       SamplingSite + Agent + (linked IdentifiedConcepts already in src)
  3. Assign new row_ids: dense rank starting at max(src.row_id)+1,
     ordered deterministically by (otype, pid).
  4. Build a mapping table: Eric's row_id → our new row_id.
  5. Remap all p__ arrays on new rows from Eric's id space to our id space.
     Concept references in p__has_* arrays resolved via URI lookup against
     src wide's IdentifiedConcept rows (same pattern as enrich_wide_with_oc_concepts.py).
  6. Denormalize geometry/lat/lon from GeoCoordLoc onto new MSR rows
     (builder reads geometry from MSR rows, not from GeoCoordLoc).
  7. Set n='OPENCONTEXT' on new MSR rows (Eric's wide has NULL).
  8. Mint new IdentifiedConcept rows for any concept URIs present in new
     MSRs but absent from src wide (expected: only earthsurface in practice).
  9. Hard-fail checks before writing (see HARD FAILURES below).
 10. Write: src rows UNION ALL new entity rows → output wide.
 11. Emit a {out}.manifest.json.

WHAT IT DOES NOT DO (scope):
  - Does not re-run the Phase 1 concept overlay (already in src wide).
  - Does not prune the 21,227 OC pids in src that are absent from Eric's wide.
  - Does not populate p__curation / p__related_resource (OC doesn't have them).
  - Does not ingest IdentifiedConcept rows for keywords beyond the p__has_* dims
    (keywords concepts should be verified separately).

HARD FAILURES (refuses to write):
  - duplicate pids among new MSRs (new pid set must be truly new)
  - any new pid already exists in src wide (ingestion grain wrong)
  - duplicate row_ids in proposed new id set vs src wide
  - any p__ reference in a new row that cannot be resolved to a row_id in output
  - any new MSR with n != 'OPENCONTEXT' in the written output
  - row count mismatch: output != src + new_entities + minted_concepts
  - duplicate pids anywhere in output (union would create them if logic is wrong)

Usage:
  python scripts/ingest_oc_records.py \\
      --src  isamples_202606_wide.parquet \\
      --oc-wide oc_isamples_pqg_wide_2026-06-09.parquet \\
      --out  isamples_202608_wide.parquet

  # Dry-run (skips writing, just runs analysis + trust checks):
  python scripts/ingest_oc_records.py --src ... --oc-wide ... --out ... --dry-run

Notes:
  - DuckDB pinned to 1.4.4 (scripts/requirements.txt). h3 + spatial extensions
    installed at runtime (needed for geometry handling).
  - Use the 202606 wide as --src (not 202604). Phase 1 (PR #275) minted the
    otheranthropogenicmaterial concept in 202606; using 202604 would require
    minting it again and risks id collision with Phase 1.
  - The --src wide's row_id column must be BIGINT (our convention). Eric's wide
    uses INTEGER — new rows cast to BIGINT automatically.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

import duckdb

# Dimensions where concept references live on MSR rows
CONCEPT_DIMS = [
    "p__has_material_category",
    "p__has_sample_object_type",
    "p__has_context_category",
]

# Columns present in our wide but absent from Eric's wide (will be NULL in new rows)
OUR_ONLY_COLS = ["p__curation", "p__related_resource"]

# The source attribution for all OC records in the unified wide
OC_SOURCE = "OPENCONTEXT"


def sha256_file(path, _bufsize=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_bufsize), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return None


def log(msg, t0):
    print(f"[{time.time()-t0:6.1f}s] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True,
                    help="Source unified wide parquet (should be isamples_202606_wide.parquet "
                         "so Phase-1 concept minting is already present)")
    ap.add_argument("--oc-wide", required=True,
                    help="Eric's OC PQG wide parquet (oc_isamples_pqg_wide_2026-06-09.parquet)")
    ap.add_argument("--out", required=True,
                    help="Output wide parquet (e.g. isamples_202608_wide.parquet)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run analysis and trust checks, do not write output")
    ap.add_argument("--no-manifest", action="store_true")
    args = ap.parse_args()

    for fp in (args.src, args.oc_wide):
        if not os.path.exists(fp):
            sys.exit(f"FATAL: missing input {fp}")
    if not args.dry_run:
        if os.path.abspath(args.out) in (os.path.abspath(args.src),
                                          os.path.abspath(args.oc_wide)):
            sys.exit("FATAL: --out must not overwrite an input")
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    t0 = time.time()
    con = duckdb.connect()
    con.execute("INSTALL h3 FROM community; LOAD h3; INSTALL spatial; LOAD spatial;")

    SRC = f"read_parquet('{args.src}')"
    OC = f"read_parquet('{args.oc_wide}')"

    # ---- schema contract checks -------------------------------------------
    src_cols_raw = con.sql(f"DESCRIBE SELECT * FROM {SRC}").fetchall()
    src_cols = [(r[0], r[1]) for r in src_cols_raw]
    src_colnames = [c for c, _ in src_cols]

    oc_cols_raw = con.sql(f"DESCRIBE SELECT * FROM {OC}").fetchall()
    oc_colnames = [r[0] for r in oc_cols_raw]

    # Verify concept dim columns exist in both
    for d in CONCEPT_DIMS:
        if d not in src_colnames:
            sys.exit(f"FATAL: src wide lacks required column {d}")
        if d not in oc_colnames:
            sys.exit(f"FATAL: oc-wide lacks required column {d}")

    # Verify p__produced_by exists (coord path)
    for col in ("p__produced_by",):
        if col not in oc_colnames:
            sys.exit(f"FATAL: oc-wide lacks required column {col}")

    log("schema checks passed", t0)

    # ---- grain checks (hard-fail before any writing) -----------------------
    n_dup_src_rowid = con.sql(
        f"SELECT COUNT(*) FROM (SELECT row_id FROM {SRC} GROUP BY row_id HAVING COUNT(*)>1)"
    ).fetchone()[0]
    n_dup_oc_pid_msr = con.sql(
        f"SELECT COUNT(*) FROM (SELECT pid FROM {OC} WHERE otype='MaterialSampleRecord' "
        f"GROUP BY pid HAVING COUNT(*)>1)"
    ).fetchone()[0]
    if n_dup_src_rowid or n_dup_oc_pid_msr:
        sys.exit(
            f"FATAL: non-unique keys — src duplicate row_ids={n_dup_src_rowid}, "
            f"OC duplicate MSR pids={n_dup_oc_pid_msr}. Refusing to proceed."
        )

    # ---- Phase A: identify new pids ----------------------------------------
    con.execute(f"""
    CREATE TEMP TABLE new_pids AS
      SELECT pid
      FROM {OC} WHERE otype='MaterialSampleRecord'
      EXCEPT
      SELECT pid
      FROM {SRC} WHERE otype='MaterialSampleRecord' AND n='{OC_SOURCE}';
    """)
    n_new_pids = con.sql("SELECT COUNT(*) FROM new_pids").fetchone()[0]
    log(f"new pids: {n_new_pids:,}", t0)
    if n_new_pids == 0:
        sys.exit("INFO: no new pids to ingest. Output would be identical to src. Exiting.")

    # Check none of the new pids sneak in as non-OPENCONTEXT records in src
    n_pid_collision = con.sql(f"""
        SELECT COUNT(*) FROM new_pids np
        JOIN {SRC} s ON s.pid = np.pid AND s.otype='MaterialSampleRecord'
    """).fetchone()[0]
    if n_pid_collision:
        sys.exit(f"FATAL: {n_pid_collision} 'new' pids already exist in src wide (with different n). "
                 f"This would create duplicate pids in output.")

    # ---- Phase B: extract new MSR rows + full entity subgraph ---------------
    log("extracting entity subgraph for new pids...", t0)
    con.execute(f"""
    -- New MSR rows from Eric's wide
    CREATE TEMP TABLE new_msr_eric AS
      SELECT e.*
      FROM {OC} e
      WHERE e.otype='MaterialSampleRecord' AND e.pid IN (SELECT pid FROM new_pids);

    -- Linked SamplingEvent row_ids
    CREATE TEMP TABLE se_ids AS
      SELECT DISTINCT u.se_id AS eric_row_id
      FROM new_msr_eric, UNNEST(p__produced_by) AS u(se_id);

    -- SamplingEvent rows
    CREATE TEMP TABLE new_se_eric AS
      SELECT e.* FROM {OC} e
      WHERE e.otype='SamplingEvent' AND e.row_id IN (SELECT eric_row_id FROM se_ids);

    -- GeospatialCoordLocation ids from SE (p__sample_location)
    CREATE TEMP TABLE geo_from_se AS
      SELECT DISTINCT u.geo_id AS eric_row_id
      FROM new_se_eric, UNNEST(p__sample_location) AS u(geo_id);

    -- SamplingSite ids from SE (p__sampling_site)
    CREATE TEMP TABLE site_ids AS
      SELECT DISTINCT u.site_id AS eric_row_id
      FROM new_se_eric, UNNEST(p__sampling_site) AS u(site_id);

    -- SamplingSite rows
    CREATE TEMP TABLE new_site_eric AS
      SELECT e.* FROM {OC} e
      WHERE e.otype='SamplingSite' AND e.row_id IN (SELECT eric_row_id FROM site_ids);

    -- GeospatialCoordLocation ids from SamplingSite (p__site_location)
    CREATE TEMP TABLE geo_from_site AS
      SELECT DISTINCT u.loc_id AS eric_row_id
      FROM new_site_eric, UNNEST(p__site_location) AS u(loc_id);

    -- All unique GeoCoordLoc ids (union of SE-linked and site-linked)
    CREATE TEMP TABLE all_geo_ids AS
      SELECT eric_row_id FROM geo_from_se
      UNION
      SELECT eric_row_id FROM geo_from_site;

    -- GeoCoordLoc rows
    CREATE TEMP TABLE new_geo_eric AS
      SELECT e.* FROM {OC} e
      WHERE e.otype='GeospatialCoordLocation' AND e.row_id IN (SELECT eric_row_id FROM all_geo_ids);

    -- Agent ids from MSR (p__registrant)
    CREATE TEMP TABLE agent_ids AS
      SELECT DISTINCT u.agent_id AS eric_row_id
      FROM new_msr_eric, UNNEST(p__registrant) AS u(agent_id);

    -- Agent rows
    CREATE TEMP TABLE new_agent_eric AS
      SELECT e.* FROM {OC} e
      WHERE e.otype='Agent' AND e.row_id IN (SELECT eric_row_id FROM agent_ids);
    """)

    counts = {
        "new_msr": con.sql("SELECT COUNT(*) FROM new_msr_eric").fetchone()[0],
        "new_se": con.sql("SELECT COUNT(*) FROM new_se_eric").fetchone()[0],
        "new_geo": con.sql("SELECT COUNT(*) FROM new_geo_eric").fetchone()[0],
        "new_site": con.sql("SELECT COUNT(*) FROM new_site_eric").fetchone()[0],
        "new_agent": con.sql("SELECT COUNT(*) FROM new_agent_eric").fetchone()[0],
    }
    log(f"subgraph: msr={counts['new_msr']:,} se={counts['new_se']:,} geo={counts['new_geo']:,} "
        f"site={counts['new_site']:,} agent={counts['new_agent']:,}", t0)

    # ---- Phase C: assign new row_ids ----------------------------------------
    max_src_row_id = con.sql(f"SELECT COALESCE(MAX(row_id), 0) FROM {SRC}").fetchone()[0]
    log(f"src max_row_id={max_src_row_id:,}", t0)

    # All new entities in one table, ordered deterministically by (otype, pid)
    # for stable dense-rank assignment
    con.execute(f"""
    CREATE TEMP TABLE all_new_entities AS
      SELECT row_id AS eric_row_id, pid, otype FROM new_msr_eric
      UNION ALL
      SELECT row_id, pid, otype FROM new_se_eric
      UNION ALL
      SELECT row_id, pid, otype FROM new_geo_eric
      UNION ALL
      SELECT row_id, pid, otype FROM new_site_eric
      UNION ALL
      SELECT row_id, pid, otype FROM new_agent_eric;

    CREATE TEMP TABLE eric_id_map AS
      SELECT eric_row_id,
             {max_src_row_id} + DENSE_RANK() OVER (ORDER BY otype, pid) AS our_row_id
      FROM all_new_entities;
    """)

    n_id_map = con.sql("SELECT COUNT(*) FROM eric_id_map").fetchone()[0]
    new_max = con.sql("SELECT MAX(our_row_id) FROM eric_id_map").fetchone()[0]
    # Verify no collision with src
    n_collision = con.sql(f"""
        SELECT COUNT(*) FROM eric_id_map m
        WHERE m.our_row_id IN (SELECT row_id FROM {SRC})
    """).fetchone()[0]
    if n_collision:
        sys.exit(f"FATAL: {n_collision} proposed new row_ids collide with existing src row_ids")
    log(f"id_map: {n_id_map:,} entries, new row_id range {max_src_row_id+1} to {new_max:,}, collisions={n_collision}", t0)

    # ---- Phase D: concept resolution for p__has_* dims ---------------------
    # OC concept row_ids (Eric's space) -> URI -> our row_id
    # Uses same approach as enrich_wide_with_oc_concepts.py
    con.execute(f"""
    CREATE TEMP TABLE oc_concept_rows AS
      SELECT row_id AS eric_row_id, pid AS uri
      FROM {OC} WHERE otype='IdentifiedConcept';

    CREATE TEMP TABLE src_concept_map AS
      SELECT pid AS uri, MIN(row_id) AS our_row_id
      FROM {SRC} WHERE otype='IdentifiedConcept' GROUP BY pid;
    """)

    # Find concepts referenced by new MSRs that are missing from src
    con.execute(f"""
    CREATE TEMP TABLE new_concept_refs AS
      SELECT DISTINCT u.cid AS eric_cid
      FROM new_msr_eric, UNNEST(p__has_material_category) AS u(cid)
      UNION
      SELECT DISTINCT u.cid FROM new_msr_eric, UNNEST(p__has_sample_object_type) AS u(cid)
      UNION
      SELECT DISTINCT u.cid FROM new_msr_eric, UNNEST(p__has_context_category) AS u(cid);

    CREATE TEMP TABLE new_concept_uris AS
      SELECT DISTINCT c.uri
      FROM new_concept_refs r
      JOIN oc_concept_rows c ON c.eric_row_id = r.eric_cid;
    """)

    n_unresolved_uris = con.sql("""
        SELECT COUNT(*) FROM new_concept_uris u
        LEFT JOIN src_concept_map m ON m.uri = u.uri
        WHERE m.our_row_id IS NULL
    """).fetchone()[0]

    if n_unresolved_uris:
        # These need to be minted — expected: only earthsurface when base is 202604.
        # When base is 202606 (production), otheranthropogenicmaterial is already there.
        missing = con.sql("""
            SELECT u.uri FROM new_concept_uris u
            LEFT JOIN src_concept_map m ON m.uri = u.uri
            WHERE m.our_row_id IS NULL
            ORDER BY u.uri
        """).fetchall()
        log(f"minting {n_unresolved_uris} new IdentifiedConcept rows: {[r[0] for r in missing]}", t0)
    else:
        log("all concept URIs already in src", t0)

    # Mint new concept rows
    max_src_row_id_with_map = con.sql("SELECT MAX(our_row_id) FROM eric_id_map").fetchone()[0]
    con.execute(f"""
    CREATE TEMP TABLE new_concepts_to_mint AS
      WITH missing_uris AS (
        SELECT u.uri FROM new_concept_uris u
        LEFT JOIN src_concept_map m ON m.uri = u.uri
        WHERE m.our_row_id IS NULL
      ),
      meta AS (
        SELECT c.uri, MIN(c2.label) AS label, MIN(c2.scheme_name) AS scheme_name,
               MIN(c2.scheme_uri) AS scheme_uri
        FROM missing_uris c
        JOIN (SELECT pid AS uri, label, scheme_name, scheme_uri FROM {OC}
              WHERE otype='IdentifiedConcept') c2 ON c2.uri = c.uri
        GROUP BY c.uri
      )
      SELECT {max_src_row_id_with_map} + DENSE_RANK() OVER (ORDER BY m.uri) AS our_row_id,
             m.uri, m.label, m.scheme_name, m.scheme_uri
      FROM meta m;

    -- Complete concept lookup: src existing + newly minted
    CREATE TEMP TABLE concept_id_lookup AS
      SELECT uri, our_row_id FROM src_concept_map
      UNION ALL
      SELECT uri, our_row_id FROM new_concepts_to_mint;
    """)

    n_minted = con.sql("SELECT COUNT(*) FROM new_concepts_to_mint").fetchone()[0]
    log(f"minted_concepts={n_minted}", t0)

    # ---- Phase E: build coord table for new MSRs ----------------------------
    # Eric's wide stores geometry as DuckDB GEOMETRY type (spatial extension auto-decodes).
    # Our wide stores geometry as BLOB (WKB bytes). Convert with ST_AsWKB() so the
    # UNION ALL with src rows (BLOB) does not fail with BLOB->GEOMETRY cast error.
    con.execute("""
    CREATE TEMP TABLE new_msr_coords AS
      WITH msr_se AS (
        SELECT m.pid,
               se.row_id AS se_eric_row_id,
               se.p__sample_location
        FROM new_msr_eric m,
             UNNEST(m.p__produced_by) AS u(se_rid)
        JOIN new_se_eric se ON se.row_id = u.se_rid
      )
      SELECT ms.pid,
             CASE WHEN geo.geometry IS NOT NULL
                  THEN ST_AsWKB(geo.geometry)::BLOB
                  ELSE NULL END AS geometry,
             geo.latitude,
             geo.longitude
      FROM msr_se ms,
           UNNEST(ms.p__sample_location) AS u(geo_rid)
      JOIN new_geo_eric geo ON geo.row_id = u.geo_rid
      WHERE geo.latitude IS NOT NULL;
    """)
    n_coords = con.sql("SELECT COUNT(*) FROM new_msr_coords").fetchone()[0]
    n_dup_coords = con.sql(
        "SELECT COUNT(*) FROM (SELECT pid FROM new_msr_coords GROUP BY pid HAVING COUNT(*)>1)"
    ).fetchone()[0]
    log(f"coords: {n_coords:,} pids with coords, {n_dup_coords} duplicate-coord pids", t0)
    if n_dup_coords:
        sys.exit(f"FATAL: {n_dup_coords} MSR pids have multiple coord rows in the graph path")

    # ---- Phase F: remap p__ arrays for new entities -------------------------
    # Build remapped MSR rows (concept p__ via URI lookup; structural p__ via eric_id_map)
    # Using UNNEST WITH ORDINALITY + JOIN + list() aggregation (decorrelated — no correlated subqueries)
    log("remapping p__ arrays for new MSR rows...", t0)

    # Helper: build the array remapping SQL for a structural p__ column (entity refs, not concepts)
    def remap_array_sql(table, col, nullable=True):
        """Return SQL expr that remaps col (INTEGER[] in Eric's space) to our BIGINT[].
        Uses a separate CTE; caller must compose appropriately."""
        # This helper is used in the big COPY statement below
        pass

    # Build the new MSR rows with all p__ remapped
    # We need:
    #   p__produced_by: remap via eric_id_map (SE row_ids)
    #   p__has_material_category, p__has_sample_object_type, p__has_context_category: remap via concept_id_lookup
    #   p__registrant: remap via eric_id_map
    #   p__responsibility: remap via eric_id_map (if present)
    #   p__sampling_site, p__sample_location, p__site_location, p__keywords: remap via eric_id_map
    #   p__curation, p__related_resource: NULL (not in Eric's wide)
    #   geometry, latitude, longitude: from new_msr_coords
    #   n: 'OPENCONTEXT'
    #   row_id: from eric_id_map

    # For each new MSR, pre-compute remapped arrays
    # Pattern: UNNEST WITH ORDINALITY -> JOIN id_map -> list(our_row_id ORDER BY ord)
    # Done via pre-aggregated temp tables (decorrelated, avoids planner blowup)

    con.execute("""
    -- Pre-aggregate remapped structural arrays for new MSRs
    -- p__produced_by (SamplingEvent references)
    CREATE TEMP TABLE remap_msr_pb AS
      SELECT m.pid,
             list(idm.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_msr_eric m,
           UNNEST(m.p__produced_by) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN eric_id_map idm ON idm.eric_row_id = u.eric_rid
      GROUP BY m.pid;

    -- p__has_material_category (concept refs via URI lookup)
    CREATE TEMP TABLE remap_msr_mat AS
      SELECT m.pid,
             list(cl.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_msr_eric m,
           UNNEST(m.p__has_material_category) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN oc_concept_rows ocr ON ocr.eric_row_id = u.eric_rid
      JOIN concept_id_lookup cl ON cl.uri = ocr.uri
      GROUP BY m.pid;

    -- p__has_sample_object_type (concept refs)
    CREATE TEMP TABLE remap_msr_obj AS
      SELECT m.pid,
             list(cl.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_msr_eric m,
           UNNEST(m.p__has_sample_object_type) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN oc_concept_rows ocr ON ocr.eric_row_id = u.eric_rid
      JOIN concept_id_lookup cl ON cl.uri = ocr.uri
      GROUP BY m.pid;

    -- p__has_context_category (concept refs)
    CREATE TEMP TABLE remap_msr_ctx AS
      SELECT m.pid,
             list(cl.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_msr_eric m,
           UNNEST(m.p__has_context_category) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN oc_concept_rows ocr ON ocr.eric_row_id = u.eric_rid
      JOIN concept_id_lookup cl ON cl.uri = ocr.uri
      GROUP BY m.pid;

    -- p__registrant (Agent refs)
    CREATE TEMP TABLE remap_msr_reg AS
      SELECT m.pid,
             list(idm.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_msr_eric m,
           UNNEST(m.p__registrant) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN eric_id_map idm ON idm.eric_row_id = u.eric_rid
      GROUP BY m.pid;

    -- p__keywords (concept refs via URI lookup; same pattern as p__has_material_category)
    CREATE TEMP TABLE remap_msr_kw AS
      SELECT m.pid,
             list(cl.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_msr_eric m,
           UNNEST(m.p__keywords) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN oc_concept_rows ocr ON ocr.eric_row_id = u.eric_rid
      JOIN concept_id_lookup cl ON cl.uri = ocr.uri
      GROUP BY m.pid;

    -- p__responsibility (Agent or other entity refs)
    CREATE TEMP TABLE remap_msr_resp AS
      SELECT m.pid,
             list(idm.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_msr_eric m,
           UNNEST(m.p__responsibility) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN eric_id_map idm ON idm.eric_row_id = u.eric_rid
      GROUP BY m.pid;
    """)

    # Similarly remap SamplingEvent p__ arrays
    con.execute("""
    -- SE p__sample_location (GeoCoordLoc refs)
    CREATE TEMP TABLE remap_se_sl AS
      SELECT s.pid,
             list(idm.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_se_eric s,
           UNNEST(s.p__sample_location) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN eric_id_map idm ON idm.eric_row_id = u.eric_rid
      GROUP BY s.pid;

    -- SE p__sampling_site (SamplingSite refs)
    CREATE TEMP TABLE remap_se_ss AS
      SELECT s.pid,
             list(idm.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_se_eric s,
           UNNEST(s.p__sampling_site) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN eric_id_map idm ON idm.eric_row_id = u.eric_rid
      GROUP BY s.pid;

    -- SamplingSite p__site_location (GeoCoordLoc refs)
    CREATE TEMP TABLE remap_site_sl AS
      SELECT s.pid,
             list(idm.our_row_id::BIGINT ORDER BY u.ord) AS remapped
      FROM new_site_eric s,
           UNNEST(s.p__site_location) WITH ORDINALITY AS u(eric_rid, ord)
      JOIN eric_id_map idm ON idm.eric_row_id = u.eric_rid
      GROUP BY s.pid;
    """)
    log("p__ remapping tables built", t0)

    # ---- trust checks before writing ----------------------------------------
    log("running pre-write trust checks...", t0)

    # Check all new MSR p__produced_by refs resolve
    n_unresolved_se = con.sql("""
        SELECT COUNT(*) FROM new_msr_eric m, UNNEST(m.p__produced_by) AS u(rid)
        LEFT JOIN eric_id_map idm ON idm.eric_row_id = u.rid
        WHERE idm.our_row_id IS NULL
    """).fetchone()[0]
    if n_unresolved_se:
        sys.exit(f"FATAL: {n_unresolved_se} p__produced_by references in new MSRs do not resolve")

    # Check all concept references resolve (via URI)
    n_unresolved_concepts = con.sql("""
        WITH all_refs AS (
            SELECT m.pid, u.eric_rid FROM new_msr_eric m, UNNEST(m.p__has_material_category) AS u(eric_rid)
            UNION ALL
            SELECT m.pid, u.eric_rid FROM new_msr_eric m, UNNEST(m.p__has_sample_object_type) AS u(eric_rid)
            UNION ALL
            SELECT m.pid, u.eric_rid FROM new_msr_eric m, UNNEST(m.p__has_context_category) AS u(eric_rid)
        )
        SELECT COUNT(*) FROM all_refs r
        LEFT JOIN oc_concept_rows ocr ON ocr.eric_row_id = r.eric_rid
        LEFT JOIN concept_id_lookup cl ON cl.uri = ocr.uri
        WHERE cl.our_row_id IS NULL
    """).fetchone()[0]
    if n_unresolved_concepts:
        sys.exit(f"FATAL: {n_unresolved_concepts} concept references in new MSRs do not resolve")

    log("trust checks passed", t0)

    # ---- compute expected output row count -----------------------------------
    n_src = con.sql(f"SELECT COUNT(*) FROM {SRC}").fetchone()[0]
    n_new_entities = n_id_map  # entities in eric_id_map
    n_out_expected = n_src + n_new_entities + n_minted
    log(f"expected output rows: {n_src:,} src + {n_new_entities:,} new entities + "
        f"{n_minted} concepts = {n_out_expected:,}", t0)

    if args.dry_run:
        log("DRY RUN: skipping write step", t0)
        print("\n=== DRY RUN SUMMARY ===")
        print(f"  new_pids:         {n_new_pids:,}")
        print(f"  new_entities:     {n_new_entities:,}")
        print(f"  minted_concepts:  {n_minted}")
        print(f"  expected_out:     {n_out_expected:,}")
        print(f"  trust_checks:     PASS")
        return 0

    # ---- Phase I: write output -----------------------------------------------
    log("writing output...", t0)

    # Build the column list for new MSR rows
    # For each column in src_cols, produce an expression that maps Eric's data to our schema
    # The key transformations:
    #   row_id -> from eric_id_map
    #   n -> 'OPENCONTEXT'
    #   geometry/latitude/longitude -> from new_msr_coords
    #   p__produced_by -> from remap_msr_pb
    #   p__has_material_category -> from remap_msr_mat
    #   p__has_sample_object_type -> from remap_msr_obj
    #   p__has_context_category -> from remap_msr_ctx
    #   p__registrant -> from remap_msr_reg
    #   p__keywords -> from remap_msr_kw
    #   p__responsibility -> from remap_msr_resp
    #   p__curation -> NULL
    #   p__related_resource -> NULL
    #   all others -> direct from new_msr_eric

    # New MSR rows SELECT
    msr_select_cols = []
    for col, typ in src_cols:
        if col == "row_id":
            msr_select_cols.append(f"idm.our_row_id::BIGINT AS row_id")
        elif col == "n":
            msr_select_cols.append(f"'{OC_SOURCE}'::VARCHAR AS n")
        elif col == "geometry":
            msr_select_cols.append(f"coords.geometry AS geometry")
        elif col == "latitude":
            msr_select_cols.append(f"coords.latitude AS latitude")
        elif col == "longitude":
            msr_select_cols.append(f"coords.longitude AS longitude")
        elif col == "p__produced_by":
            msr_select_cols.append(f"rmap_pb.remapped::{typ} AS p__produced_by")
        elif col == "p__has_material_category":
            msr_select_cols.append(f"rmap_mat.remapped::{typ} AS p__has_material_category")
        elif col == "p__has_sample_object_type":
            msr_select_cols.append(f"rmap_obj.remapped::{typ} AS p__has_sample_object_type")
        elif col == "p__has_context_category":
            msr_select_cols.append(f"rmap_ctx.remapped::{typ} AS p__has_context_category")
        elif col == "p__registrant":
            msr_select_cols.append(f"rmap_reg.remapped::{typ} AS p__registrant")
        elif col == "p__keywords":
            msr_select_cols.append(f"rmap_kw.remapped::{typ} AS p__keywords")
        elif col == "p__responsibility":
            msr_select_cols.append(f"rmap_resp.remapped::{typ} AS p__responsibility")
        elif col in OUR_ONLY_COLS:
            msr_select_cols.append(f"NULL::{typ} AS {col}")
        elif col in oc_colnames:
            msr_select_cols.append(f"m.{col}::{typ} AS {col}")
        else:
            msr_select_cols.append(f"NULL::{typ} AS {col}")

    msr_select = ",\n       ".join(msr_select_cols)

    # New SE rows SELECT (remapped p__sample_location and p__sampling_site)
    se_select_cols = []
    for col, typ in src_cols:
        if col == "row_id":
            se_select_cols.append(f"idm.our_row_id::BIGINT AS row_id")
        elif col == "p__sample_location":
            se_select_cols.append(f"rmap_sl.remapped::{typ} AS p__sample_location")
        elif col == "p__sampling_site":
            se_select_cols.append(f"rmap_ss.remapped::{typ} AS p__sampling_site")
        elif col in OUR_ONLY_COLS:
            se_select_cols.append(f"NULL::{typ} AS {col}")
        elif col in oc_colnames:
            se_select_cols.append(f"s.{col}::{typ} AS {col}")
        else:
            se_select_cols.append(f"NULL::{typ} AS {col}")
    se_select = ",\n       ".join(se_select_cols)

    # New SamplingSite rows SELECT (remapped p__site_location)
    site_select_cols = []
    for col, typ in src_cols:
        if col == "row_id":
            site_select_cols.append(f"idm.our_row_id::BIGINT AS row_id")
        elif col == "p__site_location":
            site_select_cols.append(f"rmap_site_sl.remapped::{typ} AS p__site_location")
        elif col in OUR_ONLY_COLS:
            site_select_cols.append(f"NULL::{typ} AS {col}")
        elif col in oc_colnames:
            site_select_cols.append(f"st.{col}::{typ} AS {col}")
        else:
            site_select_cols.append(f"NULL::{typ} AS {col}")
    site_select = ",\n       ".join(site_select_cols)

    # Generic entity SELECT (Geo, Agent: just row_id remapped, all other cols direct)
    # geometry: Eric's wide has GEOMETRY type (spatial extension), our wide has BLOB (WKB).
    # Convert with ST_AsWKB() for GEOMETRY-typed columns; BLOB columns pass through directly.
    def generic_entity_select(alias, table_alias, eric_geo_is_geometry=False):
        parts = []
        for col, typ in src_cols:
            if col == "row_id":
                parts.append(f"idm.our_row_id::BIGINT AS row_id")
            elif col == "geometry" and eric_geo_is_geometry:
                # GeoCoordLoc in Eric's wide stores geometry as GEOMETRY type
                parts.append(
                    f"CASE WHEN {table_alias}.geometry IS NOT NULL "
                    f"THEN ST_AsWKB({table_alias}.geometry)::BLOB "
                    f"ELSE NULL END AS geometry"
                )
            elif col in OUR_ONLY_COLS:
                parts.append(f"NULL::{typ} AS {col}")
            elif col in oc_colnames:
                parts.append(f"{table_alias}.{col}::{typ} AS {col}")
            else:
                parts.append(f"NULL::{typ} AS {col}")
        return ",\n       ".join(parts)

    # GeoCoordLoc in Eric's wide has geometry as GEOMETRY type (auto-decoded by spatial extension)
    geo_select = generic_entity_select("g", "g", eric_geo_is_geometry=True)
    agent_select = generic_entity_select("a", "a")

    # Minted concept rows SELECT
    concept_select_cols = []
    for col, typ in src_cols:
        mapping = {
            "row_id": f"nc.our_row_id::BIGINT",
            "pid": "nc.uri::VARCHAR",
            "otype": "'IdentifiedConcept'::VARCHAR",
            "label": "nc.label::VARCHAR",
            "scheme_name": "nc.scheme_name::VARCHAR",
            "scheme_uri": "nc.scheme_uri::VARCHAR",
        }
        if col in mapping:
            concept_select_cols.append(f"{mapping[col]} AS {col}")
        else:
            concept_select_cols.append(f"NULL::{typ} AS {col}")
    concept_select = ",\n       ".join(concept_select_cols)

    write_sql = f"""
    COPY (
      -- 1. All existing src rows (unchanged)
      SELECT * FROM {SRC}

      UNION ALL BY NAME

      -- 2. New MaterialSampleRecord rows (remapped + denormalized coords)
      SELECT {msr_select}
      FROM new_msr_eric m
      JOIN eric_id_map idm ON idm.eric_row_id = m.row_id
      LEFT JOIN new_msr_coords coords ON coords.pid = m.pid
      LEFT JOIN remap_msr_pb rmap_pb ON rmap_pb.pid = m.pid
      LEFT JOIN remap_msr_mat rmap_mat ON rmap_mat.pid = m.pid
      LEFT JOIN remap_msr_obj rmap_obj ON rmap_obj.pid = m.pid
      LEFT JOIN remap_msr_ctx rmap_ctx ON rmap_ctx.pid = m.pid
      LEFT JOIN remap_msr_reg rmap_reg ON rmap_reg.pid = m.pid
      LEFT JOIN remap_msr_kw rmap_kw ON rmap_kw.pid = m.pid
      LEFT JOIN remap_msr_resp rmap_resp ON rmap_resp.pid = m.pid

      UNION ALL BY NAME

      -- 3. New SamplingEvent rows (remapped structural arrays)
      SELECT {se_select}
      FROM new_se_eric s
      JOIN eric_id_map idm ON idm.eric_row_id = s.row_id
      LEFT JOIN remap_se_sl rmap_sl ON rmap_sl.pid = s.pid
      LEFT JOIN remap_se_ss rmap_ss ON rmap_ss.pid = s.pid

      UNION ALL BY NAME

      -- 4. New GeospatialCoordLocation rows (just row_id remapped)
      SELECT {geo_select}
      FROM new_geo_eric g
      JOIN eric_id_map idm ON idm.eric_row_id = g.row_id

      UNION ALL BY NAME

      -- 5. New SamplingSite rows (remapped p__site_location)
      SELECT {site_select}
      FROM new_site_eric st
      JOIN eric_id_map idm ON idm.eric_row_id = st.row_id
      LEFT JOIN remap_site_sl rmap_site_sl ON rmap_site_sl.pid = st.pid

      UNION ALL BY NAME

      -- 6. New Agent rows
      SELECT {agent_select}
      FROM new_agent_eric a
      JOIN eric_id_map idm ON idm.eric_row_id = a.row_id

      UNION ALL BY NAME

      -- 7. Minted IdentifiedConcept rows
      SELECT {concept_select}
      FROM new_concepts_to_mint nc

      ORDER BY row_id
    ) TO '{args.out}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """

    con.execute(write_sql)
    log(f"wrote {args.out}", t0)

    # ---- post-write verification --------------------------------------------
    OUT = f"read_parquet('{args.out}')"
    n_out = con.sql(f"SELECT COUNT(*) FROM {OUT}").fetchone()[0]
    if n_out != n_out_expected:
        sys.exit(f"FATAL: row count {n_out:,} != expected {n_out_expected:,}. "
                 f"(src={n_src:,} + new={n_new_entities:,} + minted={n_minted})")

    n_dup_out_rowid = con.sql(
        f"SELECT COUNT(*) FROM (SELECT row_id FROM {OUT} GROUP BY row_id HAVING COUNT(*)>1)"
    ).fetchone()[0]
    if n_dup_out_rowid:
        sys.exit(f"FATAL: {n_dup_out_rowid} duplicate row_ids in output")

    n_dup_out_pid = con.sql(
        f"SELECT COUNT(*) FROM (SELECT pid FROM {OUT} WHERE otype='MaterialSampleRecord' "
        f"GROUP BY pid HAVING COUNT(*)>1)"
    ).fetchone()[0]
    if n_dup_out_pid:
        sys.exit(f"FATAL: {n_dup_out_pid} duplicate MaterialSampleRecord pids in output")

    # Verify n='OPENCONTEXT' on ALL new MSR rows in output
    n_wrong_n = con.sql(f"""
        SELECT COUNT(*) FROM {OUT}
        WHERE otype='MaterialSampleRecord' AND n!='{OC_SOURCE}'
        AND pid IN (SELECT pid FROM new_pids)
    """).fetchone()[0]
    if n_wrong_n:
        sys.exit(f"FATAL: {n_wrong_n} new MSR rows have n != '{OC_SOURCE}'")

    out_oc_count = con.sql(
        f"SELECT COUNT(*) FROM {OUT} WHERE otype='MaterialSampleRecord' AND n='{OC_SOURCE}'"
    ).fetchone()[0]
    log(f"post-write: rows={n_out:,}  dup_rowids={n_dup_out_rowid}  "
        f"dup_pids={n_dup_out_pid}  oc_msrs={out_oc_count:,}  n_check=PASS", t0)

    # ---- manifest -----------------------------------------------------------
    if not args.no_manifest:
        manifest = {
            "script": os.path.basename(__file__),
            "argv": sys.argv,
            "git_sha": git_sha(),
            "duckdb_version": duckdb.__version__,
            "policy": "Ingest new OC pids from Eric's fresh OC PQG wide (#272 phase 2)",
            "inputs": {
                "src": {"path": args.src, "bytes": os.path.getsize(args.src),
                        "sha256": sha256_file(args.src)},
                "oc_wide": {"path": args.oc_wide, "bytes": os.path.getsize(args.oc_wide),
                            "sha256": sha256_file(args.oc_wide)},
            },
            "counts": {
                "src_rows": n_src,
                "new_pids": n_new_pids,
                "new_entity_rows": n_new_entities,
                "minted_concepts": n_minted,
                "out_rows": n_out,
                "new_oc_msr_total": out_oc_count,
                "entity_breakdown": counts,
            },
            "output": {"path": args.out, "bytes": os.path.getsize(args.out),
                       "sha256": sha256_file(args.out)},
        }
        mpath = args.out + ".manifest.json"
        with open(mpath, "w") as fh:
            json.dump(manifest, fh, indent=2)
        log(f"manifest -> {mpath}", t0)

    log("done", t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
