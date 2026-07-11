"""Substrate-vs-baseline search benchmark (#171 §3 — the data #172 consumes).

Runs an extended canonical query set against BOTH search paths (interim
baseline and ?fts=v1 substrate) and emits the per-query metrics the #172
gate evaluates: cold / warm-repeat / warm-new latencies, bytes, result
counts, top-K pids, and the hard-fail cross-checks that need no labels
(stopword Jaccard, duplicate-term identity, concept-only non-empty,
all-stopword controlled empty, no-hit).

Hand-labeled top-K overlaps and the DuckDB-FTS oracle overlaps are NULL in
this run's output — the top-10s are exported to
tests/search_benchmark_labels_TEMPLATE.json for Raymond to hand-label
(~30 min), and the oracle run is a separate offline step. The gate can't
close without them; everything else can be measured now.

Chunked execution (long runs get killed on the dev box as background jobs;
each slice fits a foreground timeout):

    BENCH_MODE=baseline  BENCH_SLICE=0:4 pytest tests/test_search_substrate_benchmark.py -s
    BENCH_MODE=substrate BENCH_SLICE=0:4 ...
    ... then merge:
    BENCH_MERGE=1 pytest tests/test_search_substrate_benchmark.py -s

Per-slice records append to tests/.bench_records_<mode>.jsonl; the merge
step computes cross-checks and writes
tests/search_substrate_benchmark_<UTC_DATE>.json.
"""
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

import pytest

from conftest import SITE_URL
from test_search_perf import (
    _collect_search_logs, _run_search, _wait_for_explorer_ready,
    _apply_source_filter, _apply_material_first_n, _wait_for_facet_settle,
)

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent

# Extended canonical set (#169 §9 + #167 set). `substrate_only` marks queries
# whose semantics only exist on the substrate path (all-common topk).
BENCH_QUERIES = [
    {"label": "single-common",    "term": "pottery",             "filters": {}},
    {"label": "single-rare",      "term": "basalt",              "filters": {}},
    {"label": "multi-term",       "term": "pottery Cyprus",      "filters": {}},
    {"label": "stopword-heavy",   "term": "pottery from Cyprus", "filters": {}},
    {"label": "duplicate-term",   "term": "pottery pottery cyprus", "filters": {}},
    {"label": "concept-ceramic",  "term": "ceramic",             "filters": {}},
    {"label": "concept-bone",     "term": "bone",                "filters": {}},
    {"label": "concept-mammal",   "term": "mammal",              "filters": {}},
    {"label": "diacritic",        "term": "Çatalhöyük",          "filters": {}},
    {"label": "no-hit",           "term": "xyzzyqqqplugh",       "filters": {}},
    {"label": "all-stopword",     "term": "a the of",            "filters": {}, "expect_controlled_empty": True},
    {"label": "all-common",       "term": "material sample",     "filters": {}, "substrate_only": True},
    {"label": "composed-source",  "term": "pottery",
     "filters": {"source_only": ["OPENCONTEXT"]}},
    {"label": "composed-source-material", "term": "pottery",
     "filters": {"source_only": ["OPENCONTEXT"], "material_first_n": 1}},
]
WARM_NEW_PROBE = "obsidian"   # fixed different-term probe after warm-up


def _explorer_url(mode: str) -> str:
    fts = "&fts=v1" if mode == "substrate" else ""
    return f"{SITE_URL}/explorer.html?perf=1{fts}"


def _panel_top_pids(page, k: int = 50) -> list:
    return page.evaluate(
        """(k) => Array.from(
               document.querySelectorAll('#samplesSection .sample-row[data-pid]'))
               .slice(0, k).map(el => el.dataset.pid)""", k)


def _search_filter_state(page) -> dict:
    return page.evaluate(
        """() => window.__searchFilter ? {
              total: window.__searchFilter.total,
              active: window.__searchFilter.active,
              substrate: !!window.__searchFilter.substrate,
              mode: window.__searchFilter.substrateMode || null,
              ignoredCommon: window.__searchFilter.ignoredCommon || [],
              expectedBytes: window.__searchFilter.expectedBytes ?? null,
              note: window.__searchFilter.note || null,
           } : null""")


def _measure(browser, query: dict, mode: str) -> dict:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        captured: list = []
        _collect_search_logs(page, captured)
        page.goto(_explorer_url(mode), wait_until="domcontentloaded", timeout=60_000)
        _wait_for_explorer_ready(page)
        filters = query.get("filters", {})
        if "source_only" in filters:
            _apply_source_filter(page, filters["source_only"])
        if "material_first_n" in filters:
            _apply_material_first_n(page, filters["material_first_n"])

        def _try_search(expected_after):
            """all-stopword/controlled-empty searches never emit the perf log
            on the substrate path (no query runs) — fall back to state."""
            try:
                return _run_search(page, query["term"], captured=captured,
                                   expected_id_after=expected_after)
            except TimeoutError:
                return {"elapsed_ms": None, "no_perf_event": True}

        if query.get("expect_controlled_empty"):
            page.locator("#sampleSearch").fill(query["term"])
            page.locator("#searchSubmitBtn").click()
            page.wait_for_timeout(4000)
            state = _search_filter_state(page)
            visible = page.evaluate(
                "() => (document.getElementById('searchResults')?.textContent || '')"
                " + ' | ' + (document.getElementById('samplesSection')?.textContent || '')")
            return {"label": query["label"], "term": query["term"], "mode": mode,
                    "controlled_empty": True, "state": state,
                    "copy_visible": ("common words" in visible.lower()),
                    }

        cold = _try_search(0)
        state = _search_filter_state(page)
        top_pids = _panel_top_pids(page)
        warm = _try_search(cold.get("id", 0))
        warm_new_entry = None
        if not query.get("substrate_only"):
            warm_new_entry = _try_search(warm.get("id", cold.get("id", 0)))
        # warm-new = a DIFFERENT term after warm-up
        page.locator("#sampleSearch").fill(WARM_NEW_PROBE)
        page.locator("#searchSubmitBtn").click()
        deadline = page.evaluate("() => Date.now()") + 90_000
        warm_new = None
        while page.evaluate("() => Date.now()") < deadline:
            for e in captured:
                if e.get("term") == WARM_NEW_PROBE:
                    warm_new = e
                    break
            if warm_new:
                break
            page.wait_for_timeout(250)
    finally:
        context.close()
    return {
        "label": query["label"], "term": query["term"], "mode": mode,
        "filters": query.get("filters", {}),
        "cold": cold, "warm_repeat": warm,
        "warm_new": warm_new,
        "state": state, "top_pids": top_pids,
    }


def _records_path(mode: str) -> pathlib.Path:
    return HERE / f".bench_records_{mode}.jsonl"


@pytest.mark.skipif(os.environ.get("BENCH_MERGE") == "1", reason="merge run")
def test_bench_slice(browser):
    mode = os.environ.get("BENCH_MODE", "substrate")
    lo, hi = (os.environ.get("BENCH_SLICE") or "0:99").split(":")
    queries = BENCH_QUERIES[int(lo):int(hi)]
    if mode == "baseline":
        queries = [q for q in queries if not q.get("substrate_only")]
    out = _records_path(mode)
    done = set()
    if out.exists():
        for line in out.read_text().splitlines():
            done.add(json.loads(line)["label"])
    with out.open("a") as f:
        for q in queries:
            if q["label"] in done:
                print(f"skip {q['label']} (already recorded)")
                continue
            rec = _measure(browser, q, mode)
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print(f"{mode} {q['label']}: cold={rec.get('cold', {}).get('elapsed_ms')}ms "
                  f"total={rec.get('state', {}) and rec['state'].get('total')}")


def _jaccard(a, b, k):
    sa, sb = set(a[:k]), set(b[:k])
    return len(sa & sb) / len(sa | sb) if (sa or sb) else None


def _tokenizer_parity() -> dict:
    """Side-channel check (#171 §3): every benchmark term tokenized
    identically by Python and JS."""
    sys.path.insert(0, str(REPO / "tools"))
    from search_tokenizer import tokenize  # noqa: E402
    terms = [q["term"] for q in BENCH_QUERIES]
    js = subprocess.run(
        ["node", "--input-type=module", "-e",
         "import {tokenize} from '" + str(REPO / "assets/js/search_tokenizer.js")
         + "'; const terms=JSON.parse(process.argv[1]);"
         + "console.log(JSON.stringify(terms.map(t=>tokenize(t))));",
         json.dumps(terms)],
        capture_output=True, text=True)
    js_tokens = json.loads(js.stdout.strip())
    py_tokens = [tokenize(t) for t in terms]
    mismatches = [t for t, a, b in zip(terms, py_tokens, js_tokens) if a != b]
    return {"pass": not mismatches, "mismatches": mismatches}


@pytest.mark.skipif(os.environ.get("BENCH_MERGE") != "1", reason="slice runs")
def test_bench_merge():
    modes = {}
    for mode in ("baseline", "substrate"):
        recs = {}
        p = _records_path(mode)
        if p.exists():
            for line in p.read_text().splitlines():
                r = json.loads(line)
                recs[r["label"]] = r
        modes[mode] = recs
    sub, base = modes["substrate"], modes["baseline"]

    def top(recs, label, k=10):
        return (recs.get(label, {}).get("top_pids") or [])[:k]

    cross = {
        "stopword_jaccard_top10_substrate":
            _jaccard(top(sub, "stopword-heavy"), top(sub, "multi-term"), 10),
        "duplicate_term_identity_substrate":
            top(sub, "duplicate-term", 50) == top(sub, "multi-term", 50),
        "concept_only_nonempty_substrate": {
            l: (sub.get(l, {}).get("state") or {}).get("total", 0)
            for l in ("concept-ceramic", "concept-bone", "concept-mammal")},
        "concept_only_top3_substrate": {
            l: top(sub, l, 3)
            for l in ("concept-ceramic", "concept-bone", "concept-mammal")},
        "no_hit_zero_both": {
            "substrate": (sub.get("no-hit", {}).get("state") or {}).get("total"),
            "baseline": (base.get("no-hit", {}).get("state") or {}).get("total")},
        "all_stopword_controlled_substrate":
            sub.get("all-stopword", {}).get("copy_visible"),
        "all_common_mode": (sub.get("all-common", {}).get("state") or {}).get("mode"),
        "all_common_total": (sub.get("all-common", {}).get("state") or {}).get("total"),
        "tokenizer_parity": _tokenizer_parity(),
    }
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    payload = {
        "site_url": SITE_URL,
        "captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "schema_version": 1,
        "index": "isamples_202608_search_index_v1",
        "modes": modes,
        "cross_checks": cross,
        "hand_labeled_overlap": None,   # awaits labels (template exported)
        "duckdb_fts_oracle_overlap": None,  # separate offline run
    }
    out = HERE / f"search_substrate_benchmark_{stamp}.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")

    # Labels template for Raymond: top-10 from BOTH paths per query.
    template = {
        q["label"]: {
            "term": q["term"],
            "substrate_top10": top(sub, q["label"]),
            "baseline_top10": top(base, q["label"]),
            "hand_labeled_top10": [],   # <- Raymond fills / edits
            "notes": "",
        }
        for q in BENCH_QUERIES
        if not q.get("expect_controlled_empty")
    }
    (HERE / "search_benchmark_labels_TEMPLATE.json").write_text(
        json.dumps(template, indent=2) + "\n")
    print(f"wrote {out.name} + labels template")
    print(json.dumps(cross, indent=2)[:2000])
