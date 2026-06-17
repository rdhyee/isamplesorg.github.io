# Facet hierarchy — Half (b): the tree UI (#281/#282)

Draft for RY + Codex review (2026-06-17). Half (a) (the data pipeline: `broader`,
`sample_facet_membership`, `facet_tree_summaries`, validator) is **merged** to fork
main (#16). This is the user-facing half — the tree facet display Eric asked for.
It touches `explorer.qmd` and rides on the now-merged #249 refactor (PR4a/b/#285).

**No `explorer.qmd` code is written until this plan is approved** (per the agreed
gate). Material-first.

---

## 0. Grounding (verified, not assumed)
- **Subtree filtering is trivial via membership.** Because `sample_facet_membership`
  already encodes each sample under *every* ancestor, selecting a parent node filters
  its whole subtree by filtering on the parent URI **alone** — no client-side
  descendant expansion. Proven on 202608: `WHERE concept_uri='earthmaterial'` →
  4,091,133 pids (= its tree count); **0** `rock` pids miss their `earthmaterial`
  ancestor. This shapes Q3 and the filter SQL below.
- Current UI (Explore-mapped): `facetFilters` cell `renderFilter()` builds a FLAT
  checkbox list per dim from `facet_summaries`; counts via `updateCrossFilteredCounts`
  (cube fast-path on `facet_cross_filter`, slow path JOIN `facets_v3`↔`lite`);
  filtering via `facetFilterSQL()` (`pid IN (SELECT … FROM facets_v3 WHERE material
  IN(…) …)`). Counts written into `.facet-count[data-facet][data-value]` spans by
  `applyFacetCounts`. No hierarchy anywhere.

---

## 1. Product decisions — recommendations (Q1, Q3, Q5)

### Q1 — migrate vs coexist → **material migrates to the tree; others stay flat; files coexist**
- **material**: its UI path moves to the hierarchy — tree render, membership counts,
  membership subtree filter.
- **context, object_type**: stay FLAT (today's `facet_summaries`/`facets_v3` path)
  until their trees ship as a fast-follow. The data already covers them, so this is a
  UI-rollout choice, not a data gap.
- `sample_facets_v3` is **retained** — still backs the samples table, search, and the
  flat context/object_type facets. Only material's *facet* path switches to membership.
- Rationale: smallest blast radius (Codex's material-first), one user-facing dim
  changes at a time, full rollback = revert the material branch.

### Q3 — parent selection UX → **normalized positive subtree-select; tri-state display; root = "All"**
- Clicking a node selects it = **filter to its subtree**, implemented as
  `concept_uri IN (selected node URIs)` against membership (the §0 insight —
  selecting `earthmaterial` already means "everything under earthmaterial").
- **Normalized POSITIVE selection only (Codex):** a checked parent means the whole
  subtree; descendants then render checked **because inherited**, not as separate
  explicit selections. When a parent is selected we drop any redundant descendant
  selections from the stored set. There is **no "parent minus child"** — membership
  SQL can't express exclusion, so partial-deselect-under-a-selected-parent is
  unsupported (it becomes explicit child-only selection instead). Tri-state
  `indeterminate` is purely a **display** state for "some children selected".
- **URL stores the normalized explicit selected node URIs** (not expanded
  descendants) — compact + stable. `?material=<uri>,<uri>` round-trips to checked
  nodes. Unknown/deprecated URIs in the param are ignored visibly (kept out of the
  selection, no crash).
- The dim **root** (`material`) renders as a non-selectable grouping label ("All
  materials") — selecting it = no filter.

### Q5 — scope → **material-first**, all-three machinery shared (context/object_type a fast-follow PR).

---

## 2. Tree rendering (replaces flat `renderFilter` for material)
- Build the tree client-side from `facet_tree_summaries` (`facet_type, concept_uri,
  parent_uri, depth, count`) — small (material = 19 nodes, depth 3). Labels from
  `vocab_labels` (already loaded).
- **First two levels unfolded** (#281): depth 0 (root, as "All") + depth 1 + depth 2
  visible; deeper nodes collapsed behind a disclosure caret; click to expand.
- **Nested + alphabetical within each level** (#282).
- Each node row reuses the existing `.facet-count[data-facet="material"]
  [data-value="<uri>"]` span shape so `applyFacetCounts` / `.recomputing` plumbing is
  unchanged; add indentation + a caret + a checkbox.
- Render once; only the count spans + caret state mutate (matches today's pattern).

## 3. Counts (membership)
- `updateCrossFilteredCounts` material branch reads membership instead of the flat
  value: `SELECT m.concept_uri, COUNT(DISTINCT m.pid) FROM membership m JOIN lite l ON
  l.pid=m.pid WHERE m.facet_type='material' <bbox> <cross-filter-other-dims>
  <search> GROUP BY m.concept_uri`. Node count = its membership count (= direct ∪
  descendants), so parent ≥ child holds automatically.
- **`COUNT(DISTINCT pid)` is non-negotiable** wherever membership joins (Codex):
  membership has an ancestor row per sample, and joining to other multi-valued dims
  multiplies rows — node counts must be distinct *samples*, never row counts.
- **Baseline counts switch too (Codex):** `applyFacetCounts('material', null)` falls
  back to `viewer._baselineCounts.material`; that must be sourced from
  `facet_tree_summaries`, not the flat `facet_summaries`, or the global-reset / cube
  fallback shows incompatible counts.
- **Perf (Codex Q6):** membership material rows ≈ a few × the flat count. Validate
  viewport-scoped latency against the live remote parquet first; **target p95 < 750ms
  after debounce** for global single-filter and viewport/search paths. If exceeded,
  add a precomputed `facet_tree_cross_filter` cube (global); bbox/search stay on the
  JOIN. Decide from the measured probe, not upfront.

## 4. Filtering (coherence contract)
- `facetFilterSQL()` material clause changes from `material IN (…)` on `facets_v3`
  to `pid IN (SELECT pid FROM membership WHERE facet_type='material' AND concept_uri
  IN (<selected nodes>))`. context/object_type clauses unchanged (flat).
- **Counts and the table filter must share one expression** (the #245 "facet ==
  table" invariant / the `FACETS_DESCRIPTION_EXPR` discipline) — a shared
  `materialMembershipPredicate(selectedNodes)` builder, or they drift.

## 5. Code structure (Codex)
- Extract a small **selected-facet state model** (currently URL/checkbox/filter-SQL/
  cross-filter all read the DOM directly): one source of truth for selected material
  nodes + tree structure, consumed by render, counts, filter SQL, URL round-trip,
  and heatmap/table.
- Put the new SQL in `assets/js/sql-builders.js` (the #249 PR3 seam) with
  `node --test` units: `materialMembershipPredicate()`, `materialTreeCountSQL()`.

## 6. Data wiring
- **Dev**: produce the 3 files (vocab_labels+broader, `sample_facet_membership`,
  `facet_tree_summaries`) into the local `docs/data` mirror the dev loop uses, so the
  UI builds + tests run locally with no prod dependency.
- **Prod (gated — RY)**: publish the versioned files to R2 (`isamples-ry`,
  `isamples_202608_facet_tree_summaries` / `_sample_facet_membership` / a
  `vocab_labels_202608` carrying `broader`). Non-cutover (additive); the live flat
  path is untouched until the material branch deploys. **This is a production-data
  write — RY authorizes/performs it.**

## 7. Tests (new Playwright specs, behind the smoke + characterization gate)
- tree renders with 2 levels unfolded; a collapsed node expands on click.
- selecting a parent filters the table to its subtree (use a known dense subtree, e.g.
  earthmaterial over a region) — coherence: legend node count == table "N match".
- parent tri-state when a single child is selected.
- `?material=<parent-uri>` deep-link → checked node + subtree-filtered table (URL round-trip).
- node counts non-additive sanity (parent ≥ children; parent ≠ Σ children where overlap).
- Reuse the headless-stable patterns from `facet-viewport.spec.js` (flyTo + facet-UI
  hydration waits), not the OJS-`raiseEvent` approach (which is flaky — see PR4b note).

## 8. Sequencing & risk
1. Dev-wire the 3 data files into the local mirror; branch off fork main.
2. Build the material tree render + state model (behavior behind a flag/feature toggle
   if useful for incremental review).
3. Wire membership counts + subtree filter; enforce the shared-predicate coherence.
4. Latency probe → decide cube vs JOIN.
5. New Playwright specs; Codex per step.
6. PR to fork main; RY review. Prod = R2 publish (gated) + (later) upstream promote.
- **Risk**: this is the biggest UI change since launch; mitigations = material-only
  scope, the merged test net (#249), shared-predicate coherence, behind the gate, and
  the flat path untouched for other dims / as rollback.

## 8a. Codex review (2026-06-17) — amendments folded in
Verdict: **approve with amendments**, with one condition — treat this as a
facet-state/query refactor, NOT only tree rendering, or it recreates the #249/#267
DOM-state coherence bugs. Folded in above: (1) normalized positive-selection
semantics [Q3]; (2) material baseline counts from `facet_tree_summaries` [§3];
(3) `COUNT(DISTINCT pid)` mandatory on membership joins [§3]; (4) centralize the
selected-facet state model before wiring SQL [§5]; (5) concrete latency budget
(p95 < 750ms) for the cube decision [§3].

**Missing considerations to handle (Codex):**
- Unknown/deprecated `?material=` URI → ignore visibly (no crash).
- `sourceImpossible` (all sources off) must still zero material counts.
- Search + material parent selection: table count, legend, heatmap, point-mode samples
  must all share the same search pid-set AND membership predicate.
- **Accessibility**: nested checkbox tree needs `role="tree"`/`treeitem` (or correct
  `aria-expanded` / `aria-checked="mixed"`), keyboard toggle, generous label hit targets.
- Document the local `docs/data` fixture mirror (Playwright helpers depend on hydrated
  facet UI + range-capable serving).
- UI must NOT visually imply children sum to the parent (counts are non-additive).

## 9. Open for RY
- Confirm Q1/Q3/Q5 recommendations above (or redirect).
- Authorize the **dev** data files in the repo's `docs/data` mirror (local only) and,
  when ready, the **prod R2 publish** (separate, gated).
- Any UI/UX preferences from Hana's mockup (#200) for the tree styling to honor.
