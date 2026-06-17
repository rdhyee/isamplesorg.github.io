/**
 * #281/#282 Half(b) increment 1 — Material facet hierarchy (preview flag).
 *
 * Verifies the `?facets=tree` preview: flag OFF leaves Material flat (unchanged);
 * flag ON renders the expandable tree and selecting a node filters the table to
 * that node's whole SUBTREE via the membership table (no client-side expansion).
 *
 * GATED: needs the hierarchy data (facet_tree_summaries / sample_facet_membership /
 * vocab_labels-with-broader). Until those are published to R2, run against the local
 * docs/data mirror:
 *   FACET_TREE_LOCAL=1 TEST_URL=http://localhost:5860 npx playwright test facet-tree
 * (the spec drives ?data_base=/data). Skipped by default so CI stays green until the
 * R2 publish; flip the skip / drop ?data_base once the files are remote.
 */
const { test, expect } = require('@playwright/test');

const LOCAL = !!process.env.FACET_TREE_LOCAL;
const DATA = LOCAL ? '&data_base=/data' : '';
const WORLD = '#v=1&lat=20&lng=0&alt=10000000';

test.describe('Material facet tree (#281/#282 preview)', () => {
  test.skip(!LOCAL, 'needs hierarchy data — run with FACET_TREE_LOCAL=1 against the docs/data mirror until R2 publish');
  test.setTimeout(150000);

  test('flag OFF → Material stays a flat list (no tree nodes)', async ({ page }) => {
    await page.goto(`/explorer.html?facets=flat${DATA}${WORLD}`);
    await page.waitForFunction(
      () => document.querySelectorAll('#materialFilterBody .facet-row[data-facet="material"]').length > 0,
      null, { timeout: 90000 });
    const treenodes = await page.evaluate(() => document.querySelectorAll('#materialFilterBody .facet-treenode').length);
    expect(treenodes).toBe(0);
  });

  test('flag ON → tree renders; selecting a parent filters the table to its subtree', async ({ page }) => {
    await page.goto(`/explorer.html?facets=tree${DATA}${WORLD}`);
    await page.waitForFunction(
      () => document.querySelectorAll('#materialFilterBody .facet-treenode').length > 0,
      null, { timeout: 90000 });

    // Tree structure: a non-selectable root group, several nodes, carets, and the
    // deepest level collapsed (first two levels unfolded, #281).
    const info = await page.evaluate(() => ({
      nodes: document.querySelectorAll('#materialFilterBody .facet-treenode').length,
      hasRoot: !!document.querySelector('#materialFilterBody .facet-treeroot'),
      carets: document.querySelectorAll('#materialFilterBody .facet-caret').length,
      collapsed: document.querySelectorAll('#materialFilterBody .facet-children.collapsed').length,
      earthmaterial: !!document.querySelector('#materialFilterBody input[value*="/earthmaterial"]'),
    }));
    expect(info.nodes).toBeGreaterThan(5);
    expect(info.hasRoot).toBe(true);
    expect(info.carets).toBeGreaterThan(0);
    expect(info.collapsed).toBeGreaterThan(0);
    expect(info.earthmaterial).toBe(true);

    // Selecting the "earthmaterial" parent must filter the table to its whole
    // subtree (membership encodes ancestors → no client expansion needed).
    await page.evaluate(() => {
      const cb = document.querySelector('#materialFilterBody input[value*="/earthmaterial"]');
      cb.checked = true;
      document.getElementById('materialFilterBody').dispatchEvent(new Event('change', { bubbles: true }));
    });
    await page.waitForFunction(
      () => /of [\d,]+\)/.test(document.getElementById('tablePageInfo')?.textContent || ''),
      null, { timeout: 60000 });
    const total = await page.evaluate(() => {
      const m = (document.getElementById('tablePageInfo')?.textContent || '').match(/of ([\d,]+)\)/);
      return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
    });
    expect(total).toBeGreaterThan(0);
  });
});
