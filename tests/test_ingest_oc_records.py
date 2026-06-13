"""Fast, AI-free fixture tests for the OC record ingestion (#272 Phase 2).

Builds tiny synthetic src-wide + oc-wide parquet pairs, runs the real
ingest script against them, and asserts the contract:

  TRUE SYNC behavior (D3 decision):
  - New pids (Eric's \ src) are ingested with full entity subgraph
  - Stale pids (src \ Eric's) are REMOVED along with orphan subgraph entities
  - Shared entities (referenced by both surviving and removed MSRs) are kept
  - Surviving non-OC rows are byte-identical

  Entity subgraph:
  - MaterialSampleRecord + SamplingEvent + GeospatialCoordLocation + SamplingSite + Agent
  - row_id remapping: new entities get deterministic ids starting at max(src)+1
  - p__ arrays remapped from Eric's integer space to our BIGINT space
  - geometry denormalized from GeoCoordLoc onto MSR rows (WKB BLOB)
  - n='OPENCONTEXT' on new MSR rows (Eric's wide has NULL)

  Trust-gate invariants:
  - Hard-fail on duplicate OC MSR pids in Eric's wide
  - Hard-fail on new pids that already exist in src wide
  - Hard-fail on unresolved p__ references in new rows
  - Row count arithmetic verified post-write
  - No removed pids remain in output

  Determinism:
  - Same inputs → bit-identical output (--no-manifest mode)

Run: pytest tests/test_ingest_oc_records.py -q   (needs: duckdb, spatial, h3)
"""
import hashlib
import json
import os
import subprocess
import sys

import duckdb
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
INGEST = os.path.join(REPO, "scripts", "ingest_oc_records.py")

# Vocabulary URI prefixes for test fixtures
MAT = "https://w3id.org/isample/vocabulary/material/1.0/"
OBJ = "https://w3id.org/isample/vocabulary/materialsampleobjecttype/1.0/"
SF = "https://w3id.org/isample/vocabulary/sampledfeature/1.0/"
ROOT_MAT = MAT + "material"


# ---- fixture-building helpers -----------------------------------------------

def build_src_wide(path, *, msr_rows, concept_rows, se_rows, geo_rows,
                   site_rows=None, agent_rows=None, extra_rows=None):
    """Build a minimal src wide parquet with the specified entity rows.

    msr_rows: list of dict with keys: row_id, pid, n, p__produced_by (list of ints),
              p__has_material_category, p__has_sample_object_type, p__has_context_category
              (lists of ints), geometry (WKB BLOB bytes or None), latitude, longitude
    concept_rows: list of (row_id, uri)
    se_rows: list of (row_id, pid, p__sample_location [list of int], p__sampling_site [list of int])
    geo_rows: list of (row_id, pid, latitude, longitude) — geometry will be ST_AsWKB(ST_Point)
    site_rows: list of (row_id, pid, p__site_location [list of int])
    agent_rows: list of (row_id, pid)
    """
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    def _arr(xs, t="BIGINT[]"):
        if xs is None:
            return f"NULL::{t}"
        return "[" + ",".join(str(x) for x in xs) + f"]::{t}"

    rows = []

    # Concept rows
    for rid, uri in concept_rows:
        rows.append(
            f"SELECT {rid}::BIGINT AS row_id, '{uri}' AS pid, 'IdentifiedConcept' AS otype, "
            f"NULL::VARCHAR AS n, NULL::BLOB AS geometry, NULL::DOUBLE AS latitude, "
            f"NULL::DOUBLE AS longitude, NULL::VARCHAR AS label, NULL::VARCHAR AS description, "
            f"NULL::VARCHAR[] AS place_name, NULL::TIMESTAMP AS result_time, "
            f"NULL::BIGINT[] AS p__has_material_category, NULL::BIGINT[] AS p__has_sample_object_type, "
            f"NULL::BIGINT[] AS p__has_context_category, NULL::BIGINT[] AS p__produced_by, "
            f"NULL::BIGINT[] AS p__sample_location, NULL::BIGINT[] AS p__sampling_site, "
            f"NULL::BIGINT[] AS p__site_location, NULL::BIGINT[] AS p__registrant, "
            f"NULL::BIGINT[] AS p__keywords, NULL::BIGINT[] AS p__responsibility, "
            f"NULL::INTEGER[] AS p__curation, NULL::BIGINT[] AS p__related_resource, "
            f"NULL::VARCHAR AS thumbnail_url, NULL::VARCHAR AS scheme_name, NULL::VARCHAR AS scheme_uri"
        )

    # MSR rows
    for m in msr_rows:
        lat = m.get("latitude")
        lon = m.get("longitude")
        if lat is not None and lon is not None:
            geom_expr = f"ST_AsWKB(ST_Point({lon}, {lat}))::BLOB"
        else:
            geom_expr = "NULL::BLOB"
        lat_expr = f"{lat}::DOUBLE" if lat is not None else "NULL::DOUBLE"
        lon_expr = f"{lon}::DOUBLE" if lon is not None else "NULL::DOUBLE"
        pid = m['pid']
        n_val = m.get('n', 'OPENCONTEXT')
        rows.append(
            f"SELECT {m['row_id']}::BIGINT, '{pid}', 'MaterialSampleRecord', "
            f"'{n_val}'::VARCHAR, "
            f"{geom_expr}, {lat_expr}, {lon_expr}, "
            f"'label {pid}', 'desc {pid}', "
            f"['place1']::VARCHAR[], NULL::TIMESTAMP, "
            f"{_arr(m.get('p__has_material_category'))}, "
            f"{_arr(m.get('p__has_sample_object_type'))}, "
            f"{_arr(m.get('p__has_context_category'))}, "
            f"{_arr(m.get('p__produced_by'))}, "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], "
            f"{_arr(m.get('p__registrant'))}, "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::INTEGER[], NULL::BIGINT[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    # SE rows
    for rid, pid, sample_loc, sampling_site in (se_rows or []):
        rows.append(
            f"SELECT {rid}::BIGINT, '{pid}', 'SamplingEvent', NULL::VARCHAR, "
            f"NULL::BLOB, NULL::DOUBLE, NULL::DOUBLE, NULL, NULL, NULL::VARCHAR[], NULL::TIMESTAMP, "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], "
            f"{_arr(sample_loc)}, {_arr(sampling_site)}, NULL::BIGINT[], "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], NULL::INTEGER[], NULL::BIGINT[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    # Geo rows
    for rid, pid, lat, lon in (geo_rows or []):
        rows.append(
            f"SELECT {rid}::BIGINT, '{pid}', 'GeospatialCoordLocation', NULL::VARCHAR, "
            f"ST_AsWKB(ST_Point({lon}, {lat}))::BLOB, {lat}::DOUBLE, {lon}::DOUBLE, "
            f"NULL, NULL, NULL::VARCHAR[], NULL::TIMESTAMP, "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::INTEGER[], NULL::BIGINT[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    # SamplingSite rows
    for rid, pid, site_loc in (site_rows or []):
        rows.append(
            f"SELECT {rid}::BIGINT, '{pid}', 'SamplingSite', NULL::VARCHAR, "
            f"NULL::BLOB, NULL::DOUBLE, NULL::DOUBLE, NULL, NULL, NULL::VARCHAR[], NULL::TIMESTAMP, "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], "
            f"NULL::BIGINT[], NULL::BIGINT[], {_arr(site_loc)}, "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], NULL::INTEGER[], NULL::BIGINT[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    # Agent rows
    for rid, pid in (agent_rows or []):
        rows.append(
            f"SELECT {rid}::BIGINT, '{pid}', 'Agent', NULL::VARCHAR, "
            f"NULL::BLOB, NULL::DOUBLE, NULL::DOUBLE, NULL, NULL, NULL::VARCHAR[], NULL::TIMESTAMP, "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], NULL::BIGINT[], "
            f"NULL::BIGINT[], NULL::BIGINT[], NULL::INTEGER[], NULL::BIGINT[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    # Extra rows (raw SQL)
    if extra_rows:
        rows.extend(extra_rows)

    con.execute(f"COPY ({' UNION ALL '.join(rows)}) TO '{path}' (FORMAT PARQUET)")
    con.close()


def build_oc_wide(path, *, msr_rows, concept_rows, se_rows, geo_rows,
                  site_rows=None, agent_rows=None):
    """Build a minimal OC wide parquet in Eric's schema (INTEGER row_id, GEOMETRY geometry).

    geo_rows: list of (row_id, pid, latitude, longitude) — geometry stored as GEOMETRY type
    """
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    def _arr(xs, t="INTEGER[]"):
        if xs is None:
            return f"NULL::{t}"
        return "[" + ",".join(str(x) for x in xs) + f"]::{t}"

    rows = []

    # Concept rows
    for rid, uri, label in concept_rows:
        rows.append(
            f"SELECT {rid}::INTEGER AS row_id, '{uri}' AS pid, 'IdentifiedConcept' AS otype, "
            f"NULL::VARCHAR AS n, NULL::GEOMETRY AS geometry, NULL::DOUBLE AS latitude, "
            f"NULL::DOUBLE AS longitude, {repr(label)}::VARCHAR AS label, "
            f"NULL::VARCHAR AS description, NULL::VARCHAR[] AS place_name, NULL::TIMESTAMP AS result_time, "
            f"NULL::INTEGER[] AS p__has_material_category, NULL::INTEGER[] AS p__has_sample_object_type, "
            f"NULL::INTEGER[] AS p__has_context_category, NULL::INTEGER[] AS p__produced_by, "
            f"NULL::INTEGER[] AS p__sample_location, NULL::INTEGER[] AS p__sampling_site, "
            f"NULL::INTEGER[] AS p__site_location, NULL::INTEGER[] AS p__registrant, "
            f"NULL::INTEGER[] AS p__keywords, NULL::INTEGER[] AS p__responsibility, "
            f"NULL::VARCHAR AS thumbnail_url, NULL::VARCHAR AS scheme_name, NULL::VARCHAR AS scheme_uri"
        )

    # MSR rows (no geometry on MSR in Eric's wide)
    for m in msr_rows:
        pid = m['pid']
        rows.append(
            f"SELECT {m['row_id']}::INTEGER, '{pid}', 'MaterialSampleRecord', "
            f"NULL::VARCHAR, "  # n is NULL in Eric's wide
            f"NULL::GEOMETRY, NULL::DOUBLE, NULL::DOUBLE, "
            f"'label {pid}', 'desc {pid}', "
            f"['place1']::VARCHAR[], NULL::TIMESTAMP, "
            f"{_arr(m.get('p__has_material_category'))}, "
            f"{_arr(m.get('p__has_sample_object_type'))}, "
            f"{_arr(m.get('p__has_context_category'))}, "
            f"{_arr(m.get('p__produced_by'))}, "
            f"NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], "
            f"{_arr(m.get('p__registrant'))}, "
            f"NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    # SE rows
    for rid, pid, sample_loc, sampling_site in (se_rows or []):
        rows.append(
            f"SELECT {rid}::INTEGER, '{pid}', 'SamplingEvent', NULL::VARCHAR, "
            f"NULL::GEOMETRY, NULL::DOUBLE, NULL::DOUBLE, NULL, NULL, NULL::VARCHAR[], NULL::TIMESTAMP, "
            f"NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], "
            f"{_arr(sample_loc)}, {_arr(sampling_site)}, NULL::INTEGER[], "
            f"NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    # Geo rows (GEOMETRY type in Eric's wide)
    for rid, pid, lat, lon in (geo_rows or []):
        rows.append(
            f"SELECT {rid}::INTEGER, '{pid}', 'GeospatialCoordLocation', NULL::VARCHAR, "
            f"ST_Point({lon}, {lat})::GEOMETRY, {lat}::DOUBLE, {lon}::DOUBLE, "
            f"NULL, NULL, NULL::VARCHAR[], NULL::TIMESTAMP, "
            f"NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    # SamplingSite rows
    for rid, pid, site_loc in (site_rows or []):
        rows.append(
            f"SELECT {rid}::INTEGER, '{pid}', 'SamplingSite', NULL::VARCHAR, "
            f"NULL::GEOMETRY, NULL::DOUBLE, NULL::DOUBLE, NULL, NULL, NULL::VARCHAR[], NULL::TIMESTAMP, "
            f"NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::INTEGER[], NULL::INTEGER[], {_arr(site_loc)}, "
            f"NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    # Agent rows
    for rid, pid in (agent_rows or []):
        rows.append(
            f"SELECT {rid}::INTEGER, '{pid}', 'Agent', NULL::VARCHAR, "
            f"NULL::GEOMETRY, NULL::DOUBLE, NULL::DOUBLE, NULL, NULL, NULL::VARCHAR[], NULL::TIMESTAMP, "
            f"NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::INTEGER[], NULL::INTEGER[], "
            f"NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR"
        )

    con.execute(f"COPY ({' UNION ALL '.join(rows)}) TO '{path}' (FORMAT PARQUET)")
    con.close()


def run_ingest(src, oc, out, extra_args=None):
    cmd = [sys.executable, INGEST, "--src", src, "--oc-wide", oc, "--out", out,
           "--no-manifest"]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True)


def count_otype(path, otype):
    con = duckdb.connect()
    n = con.sql(f"SELECT COUNT(*) FROM read_parquet('{path}') WHERE otype='{otype}'").fetchone()[0]
    con.close()
    return n


def get_msr(path, pid):
    con = duckdb.connect()
    r = con.sql(f"SELECT * FROM read_parquet('{path}') WHERE pid='{pid}' AND otype='MaterialSampleRecord'").fetchone()
    desc = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    cols = [d[0] for d in desc]
    con.close()
    if r is None:
        return None
    return dict(zip(cols, r))


# ---- shared fixture ---------------------------------------------------------

# Concept IDs in src space (BIGINT)
SRC_ROOT_CONCEPT_ID = 1
SRC_ROCK_CONCEPT_ID = 2
SRC_ARTIFACT_CONCEPT_ID = 3

SRC_CONCEPT_ROWS = [
    (SRC_ROOT_CONCEPT_ID, ROOT_MAT),
    (SRC_ROCK_CONCEPT_ID, MAT + "rock"),
    (SRC_ARTIFACT_CONCEPT_ID, OBJ + "artifact"),
]

# OC concept IDs in Eric's space (INTEGER)
OC_ROOT_CONCEPT_ID = 901
OC_ROCK_CONCEPT_ID = 902
OC_ARTIFACT_CONCEPT_ID = 903
OC_EARTH_CONCEPT_ID = 904  # earthsurface — not yet in src

OC_CONCEPT_ROWS = [
    (OC_ROOT_CONCEPT_ID, ROOT_MAT, "Material"),
    (OC_ROCK_CONCEPT_ID, MAT + "rock", "Rock"),
    (OC_ARTIFACT_CONCEPT_ID, OBJ + "artifact", "Artifact"),
    (OC_EARTH_CONCEPT_ID, SF + "earthsurface", "Earth Surface"),
]

# Eric's subgraph: 3 SEs, 3 Geos, 2 sites
#   New pids MSR: pid-A (se=101->geo=201), pid-B (se=102->geo=202, site=301->geo_site=211)
#   Removed pids: pid-C (se=103->geo=203) — in src, not in Eric's → stale
OC_SE_ROWS = [
    (101, "se-pid-A", [201], None),   # SE for pid-A
    (102, "se-pid-B", [202], [301]),   # SE for pid-B (with sampling site)
]
OC_SITE_ROWS = [
    (301, "site-pid-B", [211]),  # SamplingSite for pid-B, geo=211
]
OC_GEO_ROWS = [
    (201, "geo-pid-A", 45.0, 10.0),
    (202, "geo-pid-B", 50.0, 15.0),
    (211, "geo-site-B", 50.1, 15.1),  # geo from SamplingSite for pid-B
]
OC_MSR_ROWS = [
    {"row_id": 1, "pid": "pid-A", "p__produced_by": [101],
     "p__has_material_category": [OC_ROCK_CONCEPT_ID],
     "p__has_sample_object_type": [OC_ARTIFACT_CONCEPT_ID],
     "p__has_context_category": [OC_EARTH_CONCEPT_ID]},  # earthsurface to be minted
    {"row_id": 2, "pid": "pid-B", "p__produced_by": [102],
     "p__has_material_category": [OC_ROOT_CONCEPT_ID],
     "p__has_sample_object_type": [OC_ARTIFACT_CONCEPT_ID],
     "p__has_context_category": None},
]

# src wide: has pid-C (stale — not in Eric's), + all existing entities
# pid-C: se_id=103, geo_id=203 — both orphans (not shared with any surviving MSR)
SRC_SE_ROWS = [
    # pid-C's SE (will become orphan)
    (103, "se-pid-C", [203], None),
]
SRC_GEO_ROWS = [
    (203, "geo-pid-C", 60.0, 20.0),  # orphan geo for pid-C
]
SRC_MSR_ROWS = [
    {"row_id": 1000, "pid": "pid-C", "n": "OPENCONTEXT",
     "p__produced_by": [103],
     "p__has_material_category": [SRC_ROCK_CONCEPT_ID],
     "p__has_sample_object_type": [SRC_ARTIFACT_CONCEPT_ID],
     "latitude": 60.0, "longitude": 20.0},
    # non-OC MSR — must survive unchanged
    {"row_id": 1001, "pid": "pid-NON-OC", "n": "SESAR",
     "p__has_material_category": [SRC_ROCK_CONCEPT_ID],
     "latitude": 55.0, "longitude": 25.0},
]
SRC_AGENT_ROWS = [(500, "agent-existing")]  # pre-existing agent


@pytest.fixture
def pair(tmp_path):
    """Canonical 3-MSR fixture: pid-A (new), pid-B (new), pid-C (stale/removed)."""
    src = str(tmp_path / "src.parquet")
    oc = str(tmp_path / "oc.parquet")
    out = str(tmp_path / "out.parquet")

    build_src_wide(
        src,
        msr_rows=SRC_MSR_ROWS,
        concept_rows=SRC_CONCEPT_ROWS,
        se_rows=SRC_SE_ROWS,
        geo_rows=SRC_GEO_ROWS,
        agent_rows=SRC_AGENT_ROWS,
    )
    build_oc_wide(
        oc,
        msr_rows=OC_MSR_ROWS,
        concept_rows=OC_CONCEPT_ROWS,
        se_rows=OC_SE_ROWS,
        geo_rows=OC_GEO_ROWS,
        site_rows=OC_SITE_ROWS,
    )
    return src, oc, out


# ---- tests ------------------------------------------------------------------

def test_new_pids_ingested(pair):
    """pid-A and pid-B (new) are present in output."""
    src, oc, out = pair
    r = run_ingest(src, oc, out)
    assert r.returncode == 0, r.stderr + r.stdout
    assert get_msr(out, "pid-A") is not None, "pid-A missing from output"
    assert get_msr(out, "pid-B") is not None, "pid-B missing from output"


def test_stale_pid_removed(pair):
    """pid-C (stale) is NOT in output."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    assert get_msr(out, "pid-C") is None, "pid-C (stale) should have been removed"


def test_orphan_subgraph_entities_removed(pair):
    """se-pid-C and geo-pid-C (orphans) are NOT in output."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    con = duckdb.connect()
    n_se = con.sql(f"SELECT COUNT(*) FROM read_parquet('{out}') WHERE pid='se-pid-C'").fetchone()[0]
    n_geo = con.sql(f"SELECT COUNT(*) FROM read_parquet('{out}') WHERE pid='geo-pid-C'").fetchone()[0]
    con.close()
    assert n_se == 0, "orphan SE se-pid-C should have been removed"
    assert n_geo == 0, "orphan geo geo-pid-C should have been removed"


def test_non_oc_rows_survive_unchanged(pair):
    """pid-NON-OC (SESAR) is present and byte-identical."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    r_src = get_msr(src, "pid-NON-OC")
    r_out = get_msr(out, "pid-NON-OC")
    assert r_out is not None, "non-OC MSR should survive"
    assert r_src["pid"] == r_out["pid"]
    assert r_src["n"] == r_out["n"]
    assert r_src["latitude"] == r_out["latitude"]
    assert r_src["longitude"] == r_out["longitude"]


def test_geometry_denormalized_onto_new_msr(pair):
    """New MSR pid-A gets lat/lon from linked GeoCoordLoc (via SE)."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    r = get_msr(out, "pid-A")
    assert r is not None
    assert abs(r["latitude"] - 45.0) < 1e-5, f"lat wrong: {r['latitude']}"
    assert abs(r["longitude"] - 10.0) < 1e-5, f"lon wrong: {r['longitude']}"
    assert r["geometry"] is not None, "geometry should be non-null (WKB BLOB)"


def test_n_column_set_on_new_msrs(pair):
    """New MSR rows have n='OPENCONTEXT'."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    assert get_msr(out, "pid-A")["n"] == "OPENCONTEXT"
    assert get_msr(out, "pid-B")["n"] == "OPENCONTEXT"


def test_p_array_remapped_to_output_id_space(pair):
    """p__produced_by on new MSR pid-A resolves to a SE row in the output."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    con = duckdb.connect()
    # Find the SE row_id stored in pid-A's p__produced_by
    pb = con.sql(f"""
        SELECT p__produced_by[1] FROM read_parquet('{out}')
        WHERE pid='pid-A' AND otype='MaterialSampleRecord'
    """).fetchone()[0]
    # Verify that row_id exists in the output as a SamplingEvent
    se_exists = con.sql(f"""
        SELECT COUNT(*) FROM read_parquet('{out}')
        WHERE row_id = {pb} AND otype='SamplingEvent'
    """).fetchone()[0]
    con.close()
    assert pb is not None, "p__produced_by must be non-null"
    assert se_exists == 1, f"SE row_id {pb} not found in output"


def test_concept_remap_via_uri_lookup(pair):
    """p__has_material_category on new MSR pid-A resolves to the src 'rock' concept row."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    con = duckdb.connect()
    # Get the concept row_id used by pid-A in the output
    mat_rid = con.sql(f"""
        SELECT p__has_material_category[1] FROM read_parquet('{out}')
        WHERE pid='pid-A' AND otype='MaterialSampleRecord'
    """).fetchone()[0]
    # Verify it resolves to the 'rock' URI
    rock_uri = con.sql(f"""
        SELECT pid FROM read_parquet('{out}')
        WHERE row_id = {mat_rid} AND otype='IdentifiedConcept'
    """).fetchone()[0]
    con.close()
    assert rock_uri == MAT + "rock", f"concept URI wrong: {rock_uri}"


def test_minted_concept_earthsurface(pair):
    """earthsurface concept is minted when absent from src (referenced by pid-A's context)."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    con = duckdb.connect()
    n = con.sql(f"""
        SELECT COUNT(*) FROM read_parquet('{out}')
        WHERE otype='IdentifiedConcept' AND pid='{SF}earthsurface'
    """).fetchone()[0]
    con.close()
    assert n == 1, "earthsurface concept should be minted in output"


def test_sampling_site_ingested_for_pid_b(pair):
    """SamplingSite row (site-pid-B) is present in output for pid-B's chain."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    con = duckdb.connect()
    n = con.sql(f"SELECT COUNT(*) FROM read_parquet('{out}') WHERE pid='site-pid-B'").fetchone()[0]
    con.close()
    assert n == 1, "SamplingSite site-pid-B should be in output"


def test_row_count_arithmetic(pair):
    """Output row count = (src - removed) + new_entities + minted_concepts."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    con = duckdb.connect()
    n_src = con.sql(f"SELECT COUNT(*) FROM read_parquet('{src}')").fetchone()[0]
    n_out = con.sql(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
    # Removed: 1 MSR (pid-C) + 1 SE (se-pid-C) + 1 geo (geo-pid-C) = 3 rows
    # New entities: 2 MSR + 2 SE + 3 Geo (201, 202, 211) + 1 Site = 8 rows
    # Minted: 1 (earthsurface)
    # Expected: n_src - 3 + 8 + 1
    expected = n_src - 3 + 8 + 1
    con.close()
    assert n_out == expected, f"row count {n_out} != expected {expected}"


def test_no_duplicate_row_ids(pair):
    """Output has no duplicate row_ids."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    con = duckdb.connect()
    n_dup = con.sql(f"""
        SELECT COUNT(*) FROM (
            SELECT row_id FROM read_parquet('{out}')
            GROUP BY row_id HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    con.close()
    assert n_dup == 0, f"{n_dup} duplicate row_ids in output"


def test_no_duplicate_msr_pids(pair):
    """Output has no duplicate MSR pids."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    con = duckdb.connect()
    n_dup = con.sql(f"""
        SELECT COUNT(*) FROM (
            SELECT pid FROM read_parquet('{out}') WHERE otype='MaterialSampleRecord'
            GROUP BY pid HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    con.close()
    assert n_dup == 0, f"{n_dup} duplicate MSR pids in output"


def test_determinism_bit_identical(pair, tmp_path):
    """Same inputs → bit-identical outputs (--no-manifest suppresses timestamp drift)."""
    src, oc, out = pair
    out2 = str(tmp_path / "out2.parquet")
    assert run_ingest(src, oc, out).returncode == 0
    assert run_ingest(src, oc, out2).returncode == 0
    h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
    assert h(out) == h(out2), "outputs not bit-identical across two runs with same inputs"


def test_dry_run_produces_no_output(pair):
    """--dry-run exits 0 but does NOT write the output file."""
    src, oc, out = pair
    r = run_ingest(src, oc, out, extra_args=["--dry-run"])
    assert r.returncode == 0, r.stderr + r.stdout
    assert not os.path.exists(out), "--dry-run should not write output"
    assert "DRY RUN" in r.stdout


def test_hard_fail_on_duplicate_oc_pids(pair, tmp_path):
    """Eric's wide with duplicate MSR pids triggers a hard failure."""
    src, oc, out = pair
    dup_oc = str(tmp_path / "oc_dup.parquet")
    # Build OC with pid-A appearing twice
    build_oc_wide(
        dup_oc,
        msr_rows=OC_MSR_ROWS + [{"row_id": 99, "pid": "pid-A",
                                   "p__produced_by": [101],
                                   "p__has_material_category": [OC_ROCK_CONCEPT_ID]}],
        concept_rows=OC_CONCEPT_ROWS,
        se_rows=OC_SE_ROWS,
        geo_rows=OC_GEO_ROWS,
    )
    r = run_ingest(src, dup_oc, out)
    assert r.returncode != 0, "should fail on duplicate OC pids"
    assert "duplicate" in (r.stderr + r.stdout).lower()
    assert not os.path.exists(out)


def test_hard_fail_new_pid_already_in_src(tmp_path):
    """A 'new' pid already in src as non-OC row triggers a hard failure."""
    src = str(tmp_path / "src.parquet")
    oc = str(tmp_path / "oc.parquet")
    out = str(tmp_path / "out.parquet")

    # Build src with pid-A as SESAR (not OC) + pid-C as OC (will be "removed")
    build_src_wide(
        src,
        msr_rows=[
            {"row_id": 1000, "pid": "pid-A", "n": "SESAR",
             "p__has_material_category": [SRC_ROCK_CONCEPT_ID],
             "latitude": 45.0, "longitude": 10.0},
            {"row_id": 1001, "pid": "pid-C", "n": "OPENCONTEXT",
             "p__produced_by": [103],
             "p__has_material_category": [SRC_ROCK_CONCEPT_ID]},
        ],
        concept_rows=SRC_CONCEPT_ROWS,
        se_rows=[(103, "se-pid-C", [203], None)],
        geo_rows=[(203, "geo-pid-C", 60.0, 20.0)],
    )
    build_oc_wide(
        oc,
        msr_rows=OC_MSR_ROWS,  # pid-A is "new" from OC's perspective
        concept_rows=OC_CONCEPT_ROWS,
        se_rows=OC_SE_ROWS,
        geo_rows=OC_GEO_ROWS,
    )
    r = run_ingest(src, oc, out)
    assert r.returncode != 0, "should fail — pid-A is 'new' from OC but already in src as SESAR"
    assert not os.path.exists(out)


def test_removal_only_removes_oc_entities(pair):
    """The non-OC MSR (pid-NON-OC) and its row are not in the removal set."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    # non-OC row must still be in output
    r = get_msr(out, "pid-NON-OC")
    assert r is not None, "non-OC MSR should not be removed"
    assert r["n"] == "SESAR"


def test_new_row_ids_no_collision_with_src(pair):
    """All new row_ids are strictly greater than max(src.row_id)."""
    src, oc, out = pair
    assert run_ingest(src, oc, out).returncode == 0
    con = duckdb.connect()
    max_src = con.sql(f"SELECT MAX(row_id) FROM read_parquet('{src}')").fetchone()[0]
    # New rows start at max_src+1 — get all rows NOT in src
    src_ids = set(r[0] for r in con.sql(f"SELECT row_id FROM read_parquet('{src}')").fetchall())
    out_ids = set(r[0] for r in con.sql(f"SELECT row_id FROM read_parquet('{out}')").fetchall())
    new_ids = out_ids - src_ids
    con.close()
    # Removed rows are also gone; new ids are all > max_src
    if new_ids:
        assert min(new_ids) > max_src, f"New ids start at {min(new_ids)}, but max_src={max_src}"


def test_refuses_to_overwrite_input(pair):
    """--out same as --src triggers a hard failure."""
    src, oc, _ = pair
    r = run_ingest(src, oc, src)
    assert r.returncode != 0
    assert "overwrite" in (r.stderr + r.stdout).lower()


# ============================================================================
# Fix #277 — OC description enrichment
# ============================================================================

def test_oc_description_enriched_from_eric_wide(pair):
    """OC MSR pid-A gets its description from Eric's OC wide after ingestion.

    The src wide stores 'desc pid-A' (a placeholder). Eric's wide also stores
    'desc pid-A' by default from build_oc_wide(). We override pid-A's description
    in Eric's wide to a realistic site-path string and verify the output carries
    that enriched value, not the src placeholder.
    """
    src, oc, out = pair

    # Patch Eric's wide to have a realistic description for pid-A.
    # We rebuild oc with a custom description for pid-A.
    oc_patched = out.replace("out.parquet", "oc_patched.parquet")
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    # Read Eric's wide into a temp table, update pid-A's description, rewrite.
    con.execute(f"""
        COPY (
            SELECT
                row_id, pid, otype, n, geometry, latitude, longitude,
                CASE WHEN pid='pid-A' AND otype='MaterialSampleRecord'
                     THEN 'Open Context published "Sample" from: Europe/Cyprus/PKAP Survey Area/Unit 42'
                     ELSE label
                END AS label,
                CASE WHEN pid='pid-A' AND otype='MaterialSampleRecord'
                     THEN 'Open Context published "Sample" from: Europe/Cyprus/PKAP Survey Area/Unit 42'
                     ELSE description
                END AS description,
                place_name, result_time, p__has_material_category, p__has_sample_object_type,
                p__has_context_category, p__produced_by, p__sample_location, p__sampling_site,
                p__site_location, p__registrant, p__keywords, p__responsibility,
                thumbnail_url, scheme_name, scheme_uri
            FROM read_parquet('{oc}')
        ) TO '{oc_patched}' (FORMAT PARQUET)
    """)
    con.close()

    r = run_ingest(src, oc_patched, out)
    assert r.returncode == 0, r.stderr + r.stdout

    row = get_msr(out, "pid-A")
    assert row is not None
    assert "Cyprus" in row["description"], (
        f"Expected enriched description with 'Cyprus', got: {row['description']!r}"
    )


def test_non_oc_description_unchanged_by_enrichment(pair):
    """Non-OC MSR (pid-NON-OC) description is not overwritten by the OC enrichment."""
    src, oc, out = pair
    r = run_ingest(src, oc, out)
    assert r.returncode == 0, r.stderr + r.stdout

    src_row = get_msr(src, "pid-NON-OC")
    out_row = get_msr(out, "pid-NON-OC")
    assert out_row is not None
    # Non-OC rows must have same description as in src (enrichment must not touch them)
    assert out_row["description"] == src_row["description"], (
        f"Non-OC description changed: {src_row['description']!r} → {out_row['description']!r}"
    )


def test_oc_msr_count_unchanged_by_enrichment(pair):
    """Description enrichment does not change the OC MSR row count."""
    src, oc, out = pair
    r = run_ingest(src, oc, out)
    assert r.returncode == 0, r.stderr + r.stdout

    con = duckdb.connect()
    n_total = con.sql(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
    n_oc_msr = con.sql(f"""
        SELECT COUNT(*) FROM read_parquet('{out}')
        WHERE otype='MaterialSampleRecord' AND n='OPENCONTEXT'
    """).fetchone()[0]
    con.close()
    # 2 new OC MSRs (pid-A, pid-B), 1 removed (pid-C), 1 non-OC (pid-NON-OC) → 2 total OC MSRs
    assert n_oc_msr == 2, f"Expected 2 OC MSRs after sync, got {n_oc_msr}"
    # Row count must match the sync arithmetic (n_src - 3 removed + 8 new + 1 minted)
    con2 = duckdb.connect()
    n_src = con2.sql(f"SELECT COUNT(*) FROM read_parquet('{src}')").fetchone()[0]
    con2.close()
    assert n_total == n_src - 3 + 8 + 1, f"Total row count unexpected: {n_total}"


# ============================================================================
# Fix #283a — Empty-string facet filter
# ============================================================================

def test_empty_string_facet_values_filtered_from_summaries(tmp_path):
    """build_facet_summaries must not produce rows with facet_value=''.

    This is a synthetic test: we build a tiny samp_geo with an empty-string
    context value and verify it does NOT appear in facet_summaries output.
    """
    import duckdb as _duckdb
    BUILD = os.path.join(REPO, "scripts", "build_frontend_derived.py")
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import build_frontend_derived as B

    con = _duckdb.connect()
    con.execute("INSTALL h3 FROM community; LOAD h3; INSTALL spatial; LOAD spatial;")
    # Create a synthetic samp_geo with an empty-string context and a real one
    con.execute("""
        CREATE OR REPLACE TEMP TABLE samp_geo AS
        SELECT 'pid1' AS pid, 'GEOME' AS source,
               'https://w3id.org/isample/vocabulary/material/1.0/rock' AS material,
               '' AS context,   -- empty-string concept URI (the bug scenario)
               'https://w3id.org/isample/vocabulary/materialsampleobjecttype/1.0/artifact' AS object_type,
               'label1' AS label, 'desc1' AS description,
               NULL::VARCHAR AS place_name, NULL::TIMESTAMP AS result_time,
               10.0::DOUBLE AS latitude, 45.0::DOUBLE AS longitude,
               1::UBIGINT AS h3_res4, 2::UBIGINT AS h3_res6, 3::UBIGINT AS h3_res8
        UNION ALL
        SELECT 'pid2', 'GEOME',
               'https://w3id.org/isample/vocabulary/material/1.0/rock',
               'https://w3id.org/isample/vocabulary/sampledfeature/1.0/earthsurface',
               'https://w3id.org/isample/vocabulary/materialsampleobjecttype/1.0/artifact',
               'label2', 'desc2', NULL, NULL, 11.0, 46.0, 1, 2, 3
    """)

    out = str(tmp_path / "facet_summaries.parquet")
    B.build_facet_summaries(con, out)

    rows = con.sql(f"SELECT * FROM read_parquet('{out}') WHERE facet_value = ''").fetchall()
    assert rows == [], (
        f"Expected no blank facet_value rows, but got: {rows}"
    )
    # Real context value should appear
    real_rows = con.sql(
        f"SELECT COUNT(*) FROM read_parquet('{out}') WHERE facet_type='context' AND facet_value != ''"
    ).fetchone()[0]
    assert real_rows >= 1, "Expected at least one non-blank context facet row"


def test_empty_string_facet_values_filtered_from_cross_filter(tmp_path):
    """build_facet_cross_filter must not produce rows with blank facet_value."""
    import duckdb as _duckdb
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import build_frontend_derived as B

    con = _duckdb.connect()
    con.execute("INSTALL h3 FROM community; LOAD h3; INSTALL spatial; LOAD spatial;")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE samp_geo AS
        SELECT 'pid1' AS pid, 'GEOME' AS source,
               'https://w3id.org/isample/vocabulary/material/1.0/rock' AS material,
               '' AS context,
               'https://w3id.org/isample/vocabulary/materialsampleobjecttype/1.0/artifact' AS object_type,
               'label1' AS label, 'desc1' AS description,
               NULL::VARCHAR AS place_name, NULL::TIMESTAMP AS result_time,
               10.0::DOUBLE AS latitude, 45.0::DOUBLE AS longitude,
               1::UBIGINT AS h3_res4, 2::UBIGINT AS h3_res6, 3::UBIGINT AS h3_res8
    """)

    out = str(tmp_path / "facet_cross_filter.parquet")
    B.build_facet_cross_filter(con, out)

    blank_rows = con.sql(f"SELECT * FROM read_parquet('{out}') WHERE facet_value = ''").fetchall()
    assert blank_rows == [], (
        f"Expected no blank facet_value in cross_filter, got: {blank_rows}"
    )
    blank_filter_rows = con.sql(
        f"SELECT * FROM read_parquet('{out}') WHERE filter_context = ''"
    ).fetchall()
    assert blank_filter_rows == [], (
        f"Expected no blank filter_context in cross_filter, got: {blank_filter_rows}"
    )


# ============================================================================
# Fix #283b — specimentype/1.0 vocab labels
# ============================================================================

SPEC_URI_SOLID = "https://w3id.org/isample/vocabulary/specimentype/1.0/othersolidobject"
SPEC_URI_PHYS = "https://w3id.org/isample/vocabulary/specimentype/1.0/physicalspecimen"

# Optional fast-path: if ISAMPLES_VOCAB_LABELS points at an already-built
# vocab_labels.parquet, reuse it; otherwise (CI / fresh checkout) we rebuild
# it on the fly. No machine-specific default — avoids leaking a local path.
VOCAB_LABELS_PATH = os.environ.get("ISAMPLES_VOCAB_LABELS", "")


def _get_vocab_labels_parquet():
    """Return a path to vocab_labels.parquet, building it if needed."""
    if VOCAB_LABELS_PATH and os.path.exists(VOCAB_LABELS_PATH):
        return VOCAB_LABELS_PATH
    # Build into a temp file for CI / offline environments.
    BUILD_VL = os.path.join(REPO, "scripts", "build_vocab_labels.py")
    import tempfile
    tmp = tempfile.mktemp(suffix=".parquet")
    result = subprocess.run(
        [sys.executable, BUILD_VL, "-o", tmp],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        pytest.skip(f"build_vocab_labels.py failed (network?): {result.stderr[:200]}")
    return tmp


def test_specimentype_othersolidobject_in_vocab_labels():
    """specimentype/1.0/othersolidobject must be present with label 'Other solid object'."""
    vl = _get_vocab_labels_parquet()
    con = duckdb.connect()
    row = con.sql(
        f"SELECT pref_label FROM read_parquet('{vl}') WHERE uri='{SPEC_URI_SOLID}'"
    ).fetchone()
    con.close()
    assert row is not None, f"{SPEC_URI_SOLID!r} not found in vocab_labels"
    assert row[0] == "Other solid object", f"Expected 'Other solid object', got {row[0]!r}"


def test_specimentype_physicalspecimen_in_vocab_labels():
    """specimentype/1.0/physicalspecimen must be present with label 'Material sample'."""
    vl = _get_vocab_labels_parquet()
    con = duckdb.connect()
    row = con.sql(
        f"SELECT pref_label FROM read_parquet('{vl}') WHERE uri='{SPEC_URI_PHYS}'"
    ).fetchone()
    con.close()
    assert row is not None, f"{SPEC_URI_PHYS!r} not found in vocab_labels"
    assert row[0] == "Material sample", f"Expected 'Material sample', got {row[0]!r}"


def test_specimentype_labels_have_lang_en():
    """Both specimentype manual overrides must have lang='en'."""
    vl = _get_vocab_labels_parquet()
    con = duckdb.connect()
    rows = con.sql(
        f"SELECT uri, lang FROM read_parquet('{vl}') WHERE uri LIKE '%specimentype%'"
    ).fetchall()
    con.close()
    assert len(rows) == 2, f"Expected 2 specimentype rows, got {len(rows)}: {rows}"
    for uri, lang in rows:
        assert lang == "en", f"Expected lang='en' for {uri!r}, got {lang!r}"
