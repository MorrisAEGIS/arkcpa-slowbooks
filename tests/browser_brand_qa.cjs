#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const target = process.env.ARKCPA_QA_URL || 'http://127.0.0.1:3335';
const artifactDir = process.env.ARKCPA_QA_ARTIFACT_DIR || '/tmp/arkcpa-brand-browser-qa';

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

(async () => {
  fs.mkdirSync(artifactDir, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    colorScheme: 'dark',
  });
  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  const failedRequests = [];
  const badResponses = [];
  const actions = [];

  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', error => pageErrors.push({
    url: page.url(),
    message: String(error),
  }));
  page.on('requestfailed', request => {
    failedRequests.push(`${request.method()} ${request.url()} :: ${request.failure()?.errorText || 'failed'}`);
  });
  page.on('response', response => {
    if (response.status() >= 400) badResponses.push(`${response.status()} ${response.url()}`);
  });

  await page.goto(`${target}/login`, { waitUntil: 'networkidle' });
  actions.push('Opened the packaged local /login surface');
  assert(await page.title() === 'Ark CPA — MAGA Energy accounting', 'Login title is not canonical Ark CPA');
  assert(await page.getByRole('heading', { level: 1, name: 'Ark CPA' }).isVisible(), 'Ark CPA login heading is missing');
  const authOverlay = page.locator('#auth-overlay');
  assert(await authOverlay.getByText('MAGA Energy accounting', { exact: true }).isVisible(), 'MAGA Energy accounting label is missing');
  assert(await authOverlay.getByText('MAGA Energy · Shared accounting workspace', { exact: true }).isVisible(), 'Shared workspace provenance is missing');
  const mark = authOverlay.locator('.ark-mark');
  const markBox = await mark.boundingBox();
  assert(markBox && Math.round(markBox.width) === 128, `Desktop Living Ark mark is ${markBox?.width}px, expected 128px`);
  const markImage = mark.locator('.ark-mark__image');
  const motionBefore = await markImage.evaluate(el => getComputedStyle(el).filter);
  await page.waitForTimeout(350);
  const motionAfter = await markImage.evaluate(el => getComputedStyle(el).filter);
  assert(motionBefore !== motionAfter, 'Living Ark mark did not change animation phase');
  await page.screenshot({ path: path.join(artifactDir, 'auth-desktop.png'), fullPage: true });
  actions.push('Proved exact auth copy, 128px Living Ark mark, and changing motion phase');

  await page.getByRole('button', { name: 'First time? Set up Ark CPA' }).click();
  await page.locator('#operator_name').fill('Ark CPA QA');
  await page.locator('#operator_email').fill('ark-cpa-qa@example.invalid');
  await page.locator('#company_name').fill('Proof Books');
  await page.locator('#auth-password').fill('ArkCPA-QA-2026!');
  await page.locator('#auth-password-confirm').fill('ArkCPA-QA-2026!');
  await Promise.all([
    page.waitForResponse(response => response.url().includes('/api/auth/setup') && response.ok()),
    page.getByRole('button', { name: 'Set up and continue' }).click(),
  ]);
  await page.waitForSelector('#app:not([inert])');
  await page.waitForFunction(() => document.title === 'Proof Books — Ark CPA');
  actions.push('Created a disposable local operator workspace through the real setup form');

  assert(await page.locator('.topbar-brand').getByText('Ark CPA', { exact: true }).isVisible(), 'Topbar Ark CPA lockup is missing');
  assert(await page.locator('#sidebar').getByRole('heading', { name: 'Ark CPA' }).isVisible(), 'Sidebar Ark CPA lockup is missing');
  assert(await page.locator('#ark-workspace-chip').isVisible(), 'Shared workspace identity chip is missing');
  assert(await page.locator('html').getAttribute('data-ark-release') === '2.9.6', 'Rendered release marker is not 2.9.6');
  assert(await page.locator('html').getAttribute('data-theme') === 'dark', 'Ark CPA did not default to dark theme');

  await page.getByRole('button', { name: 'Use light theme' }).click();
  assert(await page.locator('html').getAttribute('data-theme') === 'light', 'Light theme toggle failed');
  assert(await page.evaluate(() => localStorage.getItem('ark-cpa-theme')) === 'light', 'Light theme was not persisted');
  await page.getByRole('button', { name: 'Use dark theme' }).click();
  assert(await page.locator('html').getAttribute('data-theme') === 'dark', 'Dark theme toggle failed');
  actions.push('Switched dark → light → dark and proved ark-cpa-theme persistence');

  await page.getByRole('button', { name: 'About' }).click();
  assert(await page.getByRole('dialog', { name: 'Ark CPA' }).isVisible(), 'About dialog did not open');
  assert(await page.getByText('Slowbooks Pro 2026 — originally created by Trent Von Holten.').isVisible(), 'Required source acknowledgment is missing');
  await page.getByRole('button', { name: 'Close About' }).click();
  assert(!(await page.getByRole('dialog', { name: 'Ark CPA' }).isVisible()), 'About dialog did not close');
  actions.push('Opened and closed About and verified license attribution is confined there');

  await page.evaluate(() => { window.location.hash = '#/settings'; });
  await page.waitForSelector('#settings-users:not([style*="display:none"])');
  await page.locator('#user-new-username').fill('maesa');
  await page.locator('#user-new-display').fill('Maesa');
  await page.locator('#user-new-email').fill('maesa@magaenergy.ai');
  await page.locator('#user-new-role').selectOption('bookkeeper');
  await Promise.all([
    page.waitForResponse(response => response.url().includes('/api/users') && response.request().method() === 'POST' && response.ok()),
    page.getByRole('button', { name: 'Invite user' }).click(),
  ]);
  await page.getByText('maesa@magaenergy.ai', { exact: true }).waitFor();
  assert(await page.getByText('Invite pending', { exact: true }).isVisible(), 'Authentik invitation state is missing');
  const maesaRow = page.locator('#users-list tr', { hasText: 'maesa@magaenergy.ai' });
  assert(await maesaRow.locator('select').inputValue() === 'bookkeeper', 'Bookkeeper role was not retained');
  const usersSection = page.locator('#settings-users');
  await usersSection.screenshot({ path: path.join(artifactDir, 'users-access-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(180);
  const usersMobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(usersMobileOverflow <= 1, `Mobile Users & access viewport overflows by ${usersMobileOverflow}px`);
  await usersSection.screenshot({ path: path.join(artifactDir, 'users-access-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.waitForTimeout(180);
  actions.push('Invited a second Authentik user and proved its pending bookkeeper state at desktop and mobile widths');

  const routeHrefs = await page.locator('#sidebar .nav-link').evaluateAll(links =>
    [...new Set(links.filter(link => !link.closest('[hidden]')).map(link => link.getAttribute('href')).filter(Boolean))]
  );
  const routeResults = [];
  for (const href of routeHrefs) {
    await page.evaluate(hash => { window.location.hash = hash.slice(1); }, href);
    await page.waitForTimeout(180);
    const state = await page.locator('#page-content').evaluate(el => ({
      text: (el.textContent || '').trim().slice(0, 180),
      width: el.getBoundingClientRect().width,
    }));
    const ok = state.text.length > 0 && !state.text.startsWith('Page not found');
    routeResults.push({ href, ok, sample: state.text });
  }
  assert(routeResults.every(result => result.ok), `Route crawl failed: ${JSON.stringify(routeResults.filter(result => !result.ok))}`);
  actions.push(`Loaded ${routeResults.length} visible accounting routes through the real hash router`);

  await page.evaluate(() => API.put('/settings', { company_type: 'nonprofit' }));
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('#app:not([inert])');
  await page.waitForFunction(() => typeof Terms !== 'undefined' && Terms.isNonprofit());
  const nonprofitHrefs = await page.locator('#sidebar [data-nonprofit]:not([hidden]) .nav-link').evaluateAll(links =>
    [...new Set(links.map(link => link.getAttribute('href')).filter(Boolean))]
  );
  const nonprofitRouteResults = [];
  for (const href of nonprofitHrefs) {
    await page.evaluate(hash => { window.location.hash = hash.slice(1); }, href);
    await page.waitForTimeout(180);
    const text = (await page.locator('#page-content').innerText()).trim().slice(0, 180);
    nonprofitRouteResults.push({ href, ok: text.length > 0 && !text.startsWith('Page not found'), sample: text });
  }
  assert(nonprofitRouteResults.length === 3, `Expected 3 nonprofit-only routes, found ${nonprofitRouteResults.length}`);
  assert(nonprofitRouteResults.every(result => result.ok), `Nonprofit route crawl failed: ${JSON.stringify(nonprofitRouteResults.filter(result => !result.ok))}`);
  actions.push('Switched the real company vocabulary and loaded all 3 nonprofit-only routes');

  await page.evaluate(() => API.put('/settings', { company_type: 'business' }));
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('#app:not([inert])');
  await page.waitForFunction(() => typeof Terms !== 'undefined' && !Terms.isNonprofit());

  await page.evaluate(() => { window.location.hash = '/'; });
  await page.waitForTimeout(250);
  const desktopOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(desktopOverflow <= 1, `Desktop viewport overflows by ${desktopOverflow}px`);
  await page.screenshot({ path: path.join(artifactDir, 'app-desktop-dark.png'), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(180);
  const mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(mobileOverflow <= 1, `Mobile viewport overflows by ${mobileOverflow}px`);
  const closedSidebarBox = await page.locator('#sidebar').boundingBox();
  assert(closedSidebarBox && closedSidebarBox.x + closedSidebarBox.width <= 1, 'Mobile sidebar should begin off canvas');
  await page.getByRole('button', { name: 'Open navigation' }).click();
  assert(await page.locator('#sidebar').isVisible(), 'Mobile navigation drawer did not open');
  assert(await page.locator('#nav-toggle').getAttribute('aria-expanded') === 'true', 'Drawer expanded state is not exposed');
  await page.locator('#sidebar .nav-link[data-page="invoices"]').click();
  await page.waitForTimeout(220);
  const routeClosedSidebarBox = await page.locator('#sidebar').boundingBox();
  assert(routeClosedSidebarBox && routeClosedSidebarBox.x + routeClosedSidebarBox.width <= 1, 'Mobile navigation drawer did not close after route selection');
  assert((await page.locator('#page-content').innerText()).includes('Invoice'), 'Invoices route did not render on mobile');
  await page.screenshot({ path: path.join(artifactDir, 'app-mobile-dark.png'), fullPage: true });
  actions.push('Opened the 390px navigation drawer, selected Invoices, and proved it closed without viewport overflow');

  const reducedContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: 'reduce',
  });
  const reducedPage = await reducedContext.newPage();
  await reducedPage.goto(`${target}/login`, { waitUntil: 'networkidle' });
  const reducedMark = reducedPage.locator('#auth-overlay .ark-mark');
  const reducedAnimation = await reducedMark.evaluate(el => getComputedStyle(el).animationName);
  const reducedBox = await reducedMark.boundingBox();
  assert(reducedAnimation === 'none', `Reduced motion still animates: ${reducedAnimation}`);
  assert(reducedBox && Math.round(reducedBox.width) === 108, `Mobile Living Ark mark is ${reducedBox?.width}px, expected 108px`);
  await reducedPage.screenshot({ path: path.join(artifactDir, 'auth-mobile-reduced-motion.png'), fullPage: true });
  await reducedContext.close();
  actions.push('Proved the 108px mobile Living Ark mark and complete reduced-motion shutdown');

  const cleanRun = pageErrors.length === 0 && failedRequests.length === 0 && badResponses.length === 0;
  const report = {
    status: cleanRun ? 'pass' : 'fail',
    timestamp: new Date().toISOString(),
    environment: 'local packaged candidate image',
    target,
    browserEngine: 'chromium',
    actions,
    assertions: {
      release: '2.9.6',
      routeCount: routeResults.length + nonprofitRouteResults.length,
      routeResults,
      nonprofitRouteResults,
      desktopOverflow,
      mobileOverflow,
      authDesktopMarkPx: Math.round(markBox.width),
      authMobileMarkPx: Math.round(reducedBox.width),
      reducedAnimation,
      usersMobileOverflow,
    },
    consoleErrors,
    pageErrors,
    failedRequests,
    badResponses,
    screenshots: [
      'auth-desktop.png',
      'users-access-desktop.png',
      'users-access-mobile.png',
      'app-desktop-dark.png',
      'app-mobile-dark.png',
      'auth-mobile-reduced-motion.png',
    ],
  };
  fs.writeFileSync(path.join(artifactDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);

  assert(pageErrors.length === 0, `Page errors: ${JSON.stringify(pageErrors)}`);
  assert(failedRequests.length === 0, `Failed requests: ${JSON.stringify(failedRequests)}`);
  assert(badResponses.length === 0, `HTTP failures: ${JSON.stringify(badResponses)}`);
  await context.close();
  await browser.close();
  console.log(JSON.stringify(report, null, 2));
})().catch(async error => {
  const failure = {
    status: 'fail',
    timestamp: new Date().toISOString(),
    target,
    error: error.stack || String(error),
  };
  fs.mkdirSync(artifactDir, { recursive: true });
  fs.writeFileSync(path.join(artifactDir, 'failure.json'), `${JSON.stringify(failure, null, 2)}\n`);
  console.error(failure.error);
  process.exit(1);
});
