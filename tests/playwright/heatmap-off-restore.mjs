// heatmap-off-restore.mjs — #262 mode-matrix regression harness
//
// Verifies that toggling heatmap OFF correctly (re)loads the altitude-
// appropriate marker layer.  Mirrors a1-verify.mjs: plain `chromium`,
// run with `node tests/playwright/heatmap-off-restore.mjs`.  NOT a
// .spec.js so it stays out of the default Playwright suite.
//
// Pre-requisites (same as a1-verify.mjs):
//   1. Build:  quarto render explorer.qmd
//   2. Serve:  python3 dev_server.py --dir docs --port 8076
//   3. Run:    node tests/playwright/heatmap-off-restore.mjs
//
// Set HEADLESS=1 for CI.  Set PORT=xxxx to override default 8076.

import { chromium } from 'playwright';

const PORT = process.env.PORT || '8076';
const BASE = `http://localhost:${PORT}/explorer.html?data_base=/data&debug=a1`;
const HEADLESS = process.env.HEADLESS === '1';

// Cluster altitude — well above ENTER_POINT_ALT (120 km)
const CLUSTER_ALT = 9_000_000;   // 9 000 km → res4
// Point altitude — well below ENTER_POINT_ALT
const POINT_ALT   = 8_000;       // 8 km

const TIMEOUT_INIT   = 180_000;  // cold DuckDB-WASM boot
const TIMEOUT_LOAD   =  90_000;  // cluster / point data load
const TIMEOUT_TOGGLE =  60_000;  // heatmap toggle → data settle

const browser = await chromium.launch({ headless: HEADLESS });

// ─── helpers ─────────────────────────────────────────────────────────────────

function makeUrl(lat, lng, alt, extra = '') {
    return `${BASE}&${extra}#v=1&lat=${lat}&lng=${lng}&alt=${alt}`;
}

async function waitAppLive(page, url) {
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(
        () => typeof window.a1dbg === 'function' &&
              typeof window.__a1globe === 'function' &&
              !!document.querySelector('#heatmapToggle'),
        null, { timeout: TIMEOUT_INIT });
}

async function globe(page) {
    return page.evaluate(() => window.__a1globe?.());
}

async function waitFor(page, predFn, timeout, label) {
    try {
        await page.waitForFunction(predFn, null, { timeout });
    } catch (_) {
        const g = await globe(page);
        console.error(`  TIMEOUT waiting for: ${label}`);
        console.error(`  globe state: ${JSON.stringify(g)}`);
        throw new Error(`Timeout: ${label}`);
    }
}

async function heatmapOn(page) {
    // Toggle heatmap on (if not already).
    const on = await page.evaluate(() => document.getElementById('heatmapToggle')?.checked);
    if (!on) {
        await page.click('#heatmapToggle');
    }
    // Wait for the heatmap to be reflected in __a1globe (h3Points hidden).
    await waitFor(page, () => window.__a1globe?.()?.h3PointsShown === false, TIMEOUT_TOGGLE, 'heatmap ON → h3Points hidden');
}

async function heatmapOff(page) {
    // Toggle heatmap off (if not already).
    const on = await page.evaluate(() => document.getElementById('heatmapToggle')?.checked);
    if (on) {
        await page.click('#heatmapToggle');
    }
}

let passed = 0;
let failed = 0;
const failures = [];

function assert(cond, msg, ctx) {
    if (cond) {
        console.log(`  ✓ ${msg}`);
        passed++;
    } else {
        console.error(`  ✗ ${msg}  (${JSON.stringify(ctx)})`);
        failed++;
        failures.push(msg);
    }
}

// ─── Test 1: boot with heatmap=1 at cluster altitude ─────────────────────────
// Assert heatmap visible, markers hidden.
{
    console.log('\n[T1] Boot with heatmap=1 at cluster altitude → assert heatmap shown, markers hidden');
    const page = await browser.newPage();
    try {
        const url = makeUrl(43.15, 11.40, CLUSTER_ALT, 'heatmap=1&sources=OPENCONTEXT');
        await waitAppLive(page, url);

        // Phase 1 should have loaded; give heatmap a moment to render.
        await waitFor(page,
            () => document.getElementById('heatmapToggle')?.checked === true,
            TIMEOUT_TOGGLE, 'T1: heatmap toggle checked');

        const g = await globe(page);
        assert(g?.h3PointsShown === false, 'T1: h3Points hidden while heatmap on', g);
        assert(g?.samplePointsShown === false, 'T1: samplePoints hidden while heatmap on', g);
        assert(g?.mode === 'cluster', 'T1: mode is cluster at cluster altitude', g);
        console.log(`  globe: ${JSON.stringify(g)}`);
    } finally {
        await page.close();
    }
}

// ─── Test 2: THE BUG — boot heatmap=1 at cluster alt, toggle OFF → clusters must load ─
{
    console.log('\n[T2] Boot heatmap=1 at cluster altitude, toggle OFF → H3 clusters must load (#262 regression)');
    const page = await browser.newPage();
    try {
        const url = makeUrl(43.15, 11.40, CLUSTER_ALT, 'heatmap=1&sources=OPENCONTEXT');
        await waitAppLive(page, url);

        // Wait for toggle to be checked (boot hydration).
        await waitFor(page,
            () => document.getElementById('heatmapToggle')?.checked === true,
            TIMEOUT_TOGGLE, 'T2: heatmap toggle checked at boot');

        // Turn heatmap OFF.
        await heatmapOff(page);

        // Wait for clusters to load and be visible.
        await waitFor(page,
            () => {
                const g = window.__a1globe?.();
                return g?.mode === 'cluster' &&
                       g?.h3PointsShown === true &&
                       (g?.h3PointsLen ?? 0) > 0;
            },
            TIMEOUT_LOAD, 'T2: h3Points loaded and shown after heatmap OFF');

        const g = await globe(page);
        assert(g?.mode === 'cluster', 'T2: mode remains cluster', g);
        assert(g?.h3PointsShown === true, 'T2: h3Points shown', g);
        assert((g?.h3PointsLen ?? 0) > 0, `T2: h3Points non-empty (got ${g?.h3PointsLen})`, g);
        assert(g?.samplePointsShown === false, 'T2: samplePoints still hidden', g);
        console.log(`  globe: ${JSON.stringify(g)}`);
    } finally {
        await page.close();
    }
}

// ─── Test 3: heatmap OFF at point altitude → sample points load ───────────────
{
    console.log('\n[T3] Heatmap ON at point altitude, toggle OFF → sample points load');
    const page = await browser.newPage();
    try {
        // Boot at cluster alt so we get a stable cluster-mode base; heatmap on.
        const url = makeUrl(35.0, 33.0, POINT_ALT, 'heatmap=1&sources=OPENCONTEXT');
        await waitAppLive(page, url);

        // At low altitude the app may boot directly into point mode.
        // Wait for heatmap to be checked (boot hydration) AND for the app to settle.
        await waitFor(page,
            () => document.getElementById('heatmapToggle')?.checked === true,
            TIMEOUT_TOGGLE, 'T3: heatmap toggle checked at boot');

        // Turn heatmap OFF at low altitude.
        await heatmapOff(page);

        // Should enter point mode with sample points loaded.
        await waitFor(page,
            () => {
                const g = window.__a1globe?.();
                return g?.mode === 'point' &&
                       g?.samplePointsShown === true &&
                       (g?.samplePointsLen ?? 0) > 0;
            },
            TIMEOUT_LOAD, 'T3: sample points loaded after heatmap OFF at low alt');

        const g = await globe(page);
        assert(g?.mode === 'point', 'T3: mode is point at low altitude', g);
        assert(g?.samplePointsShown === true, 'T3: samplePoints shown', g);
        assert((g?.samplePointsLen ?? 0) > 0, `T3: samplePoints non-empty (got ${g?.samplePointsLen})`, g);
        assert(g?.h3PointsShown === false, 'T3: h3Points hidden in point mode', g);
        console.log(`  globe: ${JSON.stringify(g)}`);
    } finally {
        await page.close();
    }
}

// ─── Test 4: heatmap OFF with search active → filtered point markers ──────────
{
    console.log('\n[T4] Heatmap ON with search active, toggle OFF → filtered point markers');
    const page = await browser.newPage();
    try {
        // Boot at cluster altitude, no heatmap yet.
        const url = makeUrl(43.15, 11.40, CLUSTER_ALT, 'sources=OPENCONTEXT');
        await waitAppLive(page, url);

        // Commit a search to force point mode.
        const TERM = 'bucchero';
        await page.fill('#sampleSearch', TERM);
        await page.press('#sampleSearch', 'Enter');
        // Use page.waitForFunction directly here so we can pass TERM as the arg.
        await page.waitForFunction(
            (t) => window.__searchFilter?.active === true &&
                   window.__searchFilter?.term === t &&
                   window.__searchFilter?.total > 0,
            TERM, { timeout: TIMEOUT_LOAD });

        // Wait for point mode with filtered sample points.
        await waitFor(page,
            () => {
                const g = window.__a1globe?.();
                return g?.mode === 'point' && (g?.samplePointsLen ?? 0) > 0;
            },
            TIMEOUT_LOAD, 'T4: point mode with samples after search');

        // Now enable heatmap.
        await heatmapOn(page);

        // With heatmap on and search active, markers should be hidden.
        const gHeat = await globe(page);
        assert(gHeat?.samplePointsShown === false, 'T4: samplePoints hidden while heatmap+search on', gHeat);

        // Toggle heatmap off — search is still active, must stay in point mode with filtered points.
        await heatmapOff(page);

        await waitFor(page,
            () => {
                const g = window.__a1globe?.();
                return g?.mode === 'point' &&
                       g?.samplePointsShown === true &&
                       (g?.samplePointsLen ?? 0) > 0;
            },
            TIMEOUT_LOAD, 'T4: filtered point markers after heatmap OFF + active search');

        const g = await globe(page);
        assert(g?.mode === 'point', 'T4: still in point mode (search forces it)', g);
        assert(g?.samplePointsShown === true, 'T4: samplePoints shown', g);
        assert((g?.samplePointsLen ?? 0) > 0, `T4: samplePoints non-empty (got ${g?.samplePointsLen})`, g);
        console.log(`  globe: ${JSON.stringify(g)}`);
    } finally {
        await page.close();
    }
}

// ─── Test 5: normal cluster→point transition (no heatmap) still works ─────────
{
    console.log('\n[T5] Normal (no-heatmap) cluster boot → heatmap ON → OFF → clusters still show');
    const page = await browser.newPage();
    try {
        const url = makeUrl(43.15, 11.40, CLUSTER_ALT, 'sources=OPENCONTEXT');
        await waitAppLive(page, url);

        // Wait for phase1 cluster data to load.
        await waitFor(page,
            () => {
                const g = window.__a1globe?.();
                return g?.mode === 'cluster' &&
                       g?.h3PointsShown === true &&
                       (g?.h3PointsLen ?? 0) > 0;
            },
            TIMEOUT_LOAD, 'T5: initial cluster load');

        const gBefore = await globe(page);
        assert(gBefore?.h3PointsShown === true, 'T5: clusters shown before heatmap', gBefore);

        // Heatmap on → hides clusters.
        await heatmapOn(page);
        const gHeat = await globe(page);
        assert(gHeat?.h3PointsShown === false, 'T5: clusters hidden while heatmap on', gHeat);

        // Heatmap off → clusters must come back.
        await heatmapOff(page);
        await waitFor(page,
            () => {
                const g = window.__a1globe?.();
                return g?.h3PointsShown === true && (g?.h3PointsLen ?? 0) > 0;
            },
            TIMEOUT_LOAD, 'T5: clusters restored after heatmap OFF');

        const gAfter = await globe(page);
        assert(gAfter?.h3PointsShown === true, 'T5: clusters shown after heatmap off', gAfter);
        assert((gAfter?.h3PointsLen ?? 0) > 0, `T5: h3PointsLen > 0 (got ${gAfter?.h3PointsLen})`, gAfter);
        console.log(`  globe: ${JSON.stringify(gAfter)}`);
    } finally {
        await page.close();
    }
}

// ─── Test 6: heatmap ON still suppresses markers ─────────────────────────────
{
    console.log('\n[T6] Enabling heatmap suppresses h3Points (regression: heatmap ON must still hide markers)');
    const page = await browser.newPage();
    try {
        const url = makeUrl(43.15, 11.40, CLUSTER_ALT, 'sources=OPENCONTEXT');
        await waitAppLive(page, url);

        // Wait for clusters to show.
        await waitFor(page,
            () => (window.__a1globe?.()?.h3PointsLen ?? 0) > 0,
            TIMEOUT_LOAD, 'T6: initial cluster load');

        await heatmapOn(page);

        const g = await globe(page);
        assert(g?.h3PointsShown === false, 'T6: h3Points hidden after heatmap ON', g);
        assert(g?.samplePointsShown === false, 'T6: samplePoints hidden after heatmap ON', g);
        console.log(`  globe: ${JSON.stringify(g)}`);
    } finally {
        await page.close();
    }
}

// ─── summary ──────────────────────────────────────────────────────────────────

await browser.close();

console.log(`\n${'─'.repeat(60)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failures.length) {
    console.error('Failures:');
    failures.forEach(f => console.error(`  - ${f}`));
    process.exit(1);
} else {
    console.log('All assertions passed.');
}
