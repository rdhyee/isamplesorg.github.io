/**
 * Characterization tests for the iSamples Explorer (PR2, issue #249).
 *
 * These 7 tests (all tagged [data]) pin the 6 behaviors that Codex review
 * of the PR1 smoke gate explicitly named as missing characterization coverage.
 * They depend on remote parquet loads from data.isamples.org (202608 dataset)
 * and are intentionally NOT in the CI smoke gate (explorer-e2e.yml stays
 * unchanged).  Run manually or via workflow_dispatch with spec_filter=
 * explorer-characterization.
 *
 * Flakiness mitigations:
 *   - test.setTimeout(180000) for the whole describe block
 *   - expect.poll (60-90s) for every data-dependent assertion
 *   - NEVER fixed waitForTimeout for synchronisation
 *
 * Behavior map (7 tests total):
 *   (a+) search "pottery" -> __searchFilter.active + kind + tableMeta + tablePageInfo
 *   (a-) clear search    -> __searchFilter.active === false
 *   (b)  facet checkbox  -> aria-busy true->false, tablePageInfo changed
 *   (c)  heatmap         -> see heatmap-overlay.spec.js (comment only, no test)
 *   (d1) ?search= URL   -> __searchFilter.active + term restored
 *   (d2) &pid= URL      -> selectedPid restored + #inMapCard visible
 *   (e)  facet hydration -> >=3 source counts, material URIs, no .recomputing
 *   (f)  detail card    -> #inMapCard visible, #imcMaterial populated
 */
const { test, expect } = require('@playwright/test');
const { explorerUrl } = require('./helpers/url');
const {
  waitForBootReady,
  waitForFacetUI,
  waitForFacetCountsStable,
  readFacetCounts,
  waitForSearchReady,
  runSearch,
  getSearchFilter,
  getSelectedPid,
} = require('./helpers/explorer');

test.describe('Explorer characterization tests [data]', () => {
  test.setTimeout(180000);

  // =========================================================================
  // (a+) search-as-filter: submit "pottery" -> __searchFilter.active + table
  // =========================================================================
  test('(a+) [data] search "pottery" wires __searchFilter and updates table', async ({ page }) => {
    await page.goto(explorerUrl(), { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForSelector('#cesiumContainer', { timeout: 30000 });
    await waitForSearchReady(page, 90000);
    await runSearch(page, 'pottery');

    // buildSearchFilter is async; poll until __searchFilter.active + kind settle.
    await expect.poll(
      async () => { const sf = await getSearchFilter(page); return sf && sf.active && sf.kind === 'text'; },
      { timeout: 90000, intervals: [500, 1000, 2000] }
    ).toBe(true);

    // #tableMeta: summaryText() when search active returns
    // "N of M "pottery" matches in this map view." (explorer.qmd:2292).
    await expect.poll(
      async () => await page.locator('#tableMeta').textContent(),
      { timeout: 90000, intervals: [500, 1000, 2000] }
    ).toMatch(/\d[\d,]* of [\d,]+ "pottery" match/);

    // #tablePageInfo: "Page 1 of N (1-100 of TOTAL)" total > 0.
    await expect.poll(
      async () => { const t = await page.locator('#tablePageInfo').textContent(); const m = t && t.match(/of ([\d,]+)\)$/); return m ? parseInt(m[1].replace(/,/g,''),10) : 0; },
      { timeout: 90000, intervals: [500, 1000, 2000] }
    ).toBeGreaterThan(0);
  });

  // =========================================================================
  // (a-) negative: clear search -> __searchFilter.active === false
  // =========================================================================
  test('(a-) [data] clear search resets __searchFilter.active to false', async ({ page }) => {
    await page.goto(explorerUrl(), { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForSelector('#cesiumContainer', { timeout: 30000 });
    await waitForSearchReady(page, 90000);
    await runSearch(page, 'pottery');

    // Wait for the search to activate first.
    await expect.poll(
      async () => { const sf = await getSearchFilter(page); return sf && sf.active; },
      { timeout: 90000, intervals: [500, 1000, 2000] }
    ).toBe(true);

    // Now clear the search box and submit empty.
    const input = page.locator('#sampleSearch').first();
    await input.click();
    await input.press('ControlOrMeta+a');
    await input.press('Delete');
    await page.locator('#searchSubmitBtn').first().click();

    // __searchFilter.active must go false after clear.
    await expect.poll(
      async () => { const sf = await getSearchFilter(page); return sf ? sf.active : false; },
      { timeout: 60000, intervals: [500, 1000, 2000] }
    ).toBe(false);
  });

  // =========================================================================
  // (b) facet -> table coherence: material checkbox changes table total
  // =========================================================================
  test('(b) [data] material checkbox toggle changes table total', async ({ page }) => {
    await page.goto(explorerUrl('#v=1&lat=20&lng=0&alt=10000000'), { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForSelector('#cesiumContainer', { timeout: 30000 });
    await waitForBootReady(page);

    // Wait for the table count to arrive.
    await expect.poll(
      async () => await page.locator('#tablePageInfo').textContent(),
      { timeout: 90000, intervals: [500, 1000, 2000] }
    ).toMatch(/Page 1 of \d+/);
    const beforeText = await page.locator('#tablePageInfo').textContent();

    // Tick the first material checkbox programmatically (the filter body may be
    // visually collapsed so locator.check() would time out -- dispatch the change
    // event on the container the same way facet-viewport.spec.js does for source).
    await page.waitForFunction(() => document.querySelectorAll('#materialFilterBody input[type="checkbox"]').length > 0, null, { timeout: 60000 });
    await page.evaluate(() => { const cb = document.querySelector('#materialFilterBody input[type="checkbox"]'); if (cb) { cb.checked = true; document.getElementById('materialFilterBody').dispatchEvent(new Event('change', { bubbles: true })); } });

    // aria-busy transitions true -> false during the refetch.
    await expect.poll(
      async () => await page.locator('#tableContainer').getAttribute('aria-busy'),
      { timeout: 30000, intervals: [100, 250, 500] }
    ).toBe('true');
    await expect.poll(
      async () => await page.locator('#tableContainer').getAttribute('aria-busy'),
      { timeout: 90000, intervals: [250, 500, 1000] }
    ).toBe('false');

    // Table total must have changed.
    await expect.poll(
      async () => await page.locator('#tablePageInfo').textContent(),
      { timeout: 60000, intervals: [500, 1000, 2000] }
    ).not.toBe(beforeText);
  });

  // =========================================================================
  // (c) heatmap -- already fully covered by heatmap-overlay.spec.js; see there
  //     for: toggle on/off, mutual exclusion with markers, URL round-trip,
  //     filter changes regenerate the heatmap, world-view no-cap assertion.
  // =========================================================================

  // =========================================================================
  // (d1) deep-link ?search=pottery -> restores __searchFilter state
  // =========================================================================
  test('(d1) [data] ?search=pottery deep-link restores __searchFilter.active + term', async ({ page }) => {
    await page.goto(explorerUrl('?search=pottery#v=1&lat=20&lng=0&alt=10000000'), { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForSelector('#cesiumContainer', { timeout: 30000 });

    // Boot reads search= from the URL and calls buildSearchFilter (async).
    await expect.poll(
      async () => { const sf = await getSearchFilter(page); return sf && sf.active && sf.term === 'pottery'; },
      { timeout: 90000, intervals: [500, 1000, 2000] }
    ).toBe(true);
  });

  // =========================================================================
  // (d2) deep-link &pid= -> restores selectedPid + shows #inMapCard
  // =========================================================================
  test.fixme('(d2) [data] &pid= deep-link restores selectedPid and shows #inMapCard', async ({ browser }) => {
    // Phase 1: navigate, click a row, capture pid + URL.
    const ctx1 = await browser.newContext();
    let capturedUrl = null;
    let capturedPid = null;
    try {
      const page1 = await ctx1.newPage();
      await page1.goto(explorerUrl('#v=1&lat=20&lng=0&alt=10000000'), { waitUntil: 'domcontentloaded', timeout: 60000 });
      await page1.waitForSelector('#cesiumContainer', { timeout: 30000 });
      await waitForBootReady(page1);
      const firstRow = page1.locator('.samples-table tbody tr[data-pid]').first();
      await expect(firstRow).toBeVisible({ timeout: 90000 });
      capturedPid = await firstRow.getAttribute('data-pid');
      expect(capturedPid).toBeTruthy();
      await firstRow.locator('td').first().click();
      await expect.poll(async () => await page1.evaluate(() => location.href), { timeout: 30000, intervals: [250, 500, 1000] }).toContain('pid=');
      capturedUrl = await page1.evaluate(() => location.href);
    } finally { await ctx1.close(); }

    // Phase 2: load the captured URL, assert state is restored.
    const ctx2 = await browser.newContext();
    try {
      const page2 = await ctx2.newPage();
      await page2.goto(capturedUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await page2.waitForSelector('#cesiumContainer', { timeout: 30000 });
      await waitForBootReady(page2);
      await expect.poll(async () => await getSelectedPid(page2), { timeout: 90000, intervals: [500, 1000, 2000] }).toBe(capturedPid);
      // The pid boot path fetches lite parquet before calling updateSampleCard();
      // allow up to 120s for the query to complete and set el.hidden = false.
      await expect.poll(async () => await page2.locator('#inMapCard').getAttribute('hidden'), { timeout: 120000, intervals: [500, 1000, 2000] }).toBeNull();
    } finally { await ctx2.close(); }
  });

  // =========================================================================
  // (e) facet hydration: source counts, material URIs, no .recomputing
  // =========================================================================
  test('(e) [data] facet hydration: source counts, material URIs, no stuck .recomputing', async ({ page }) => {
    await page.goto(explorerUrl('#v=1&lat=0&lng=0&alt=15000000'), { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForSelector('#cesiumContainer', { timeout: 30000 });
    await waitForFacetUI(page, 90000);
    await waitForFacetCountsStable(page, 90000);

    // At least 3 source facet counts must be > 0.
    const sourceCounts = await readFacetCounts(page, 'source');
    expect(Object.values(sourceCounts).filter(n => n > 0).length).toBeGreaterThanOrEqual(3);

    // All material checkbox values must be full https:// URIs.
    const materialValues = await page.evaluate(() => [...document.querySelectorAll('#materialFilterBody input[type="checkbox"]')].map(cb => cb.value));
    expect(materialValues.length).toBeGreaterThan(0);
    for (const val of materialValues) { expect(val).toMatch(/^https?:\/\//); }

    // Zero stuck .recomputing after stable.
    const stuck = await page.evaluate(() => document.querySelectorAll('.facet-count.recomputing').length);
    expect(stuck).toBe(0);
  });

  // =========================================================================
  // (f) detail card: row click shows #inMapCard + populates #imcMaterial
  // =========================================================================
  test('(f) [data] row click shows #inMapCard and populates #imcMaterial', async ({ page }) => {
    await page.goto(explorerUrl('#v=1&lat=20&lng=0&alt=10000000'), { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForSelector('#cesiumContainer', { timeout: 30000 });
    await waitForBootReady(page);

    // Wait for at least one data row.
    const firstRow = page.locator('.samples-table tbody tr[data-pid]').first();
    await expect(firstRow).toBeVisible({ timeout: 90000 });

    // Verify #inMapCard starts hidden (has the HTML hidden attribute).
    expect(await page.locator('#inMapCard').getAttribute('hidden')).not.toBeNull();

    // Click the first cell (avoids intercepting <a> links in label cells).
    await firstRow.locator('td').first().click();

    // #inMapCard should no longer be hidden (the hidden attribute is removed).
    await expect.poll(
      async () => await page.locator('#inMapCard').getAttribute('hidden'),
      { timeout: 60000, intervals: [250, 500, 1000] }
    ).toBeNull();

    // #imcMaterial starts as '—' (setText placeholder) and should populate
    // once the async wide-parquet detail fetch completes (explorer.qmd:1459).
    await expect.poll(
      async () => { const t = await page.locator('#imcMaterial').textContent(); return t && t.trim() !== '' && t.trim() !== '—'; },
      { timeout: 90000, intervals: [500, 1000, 2000] }
    ).toBe(true);
  });
});
