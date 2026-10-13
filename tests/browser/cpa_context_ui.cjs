/* Isolated real-browser regression: actual shipped scripts, synthetic APIs.
 * Run: PLAYWRIGHT_MODULE=/path/to/playwright node tests/browser/cpa_context_ui.cjs
 * Optional CPA_UI_ARTIFACT_DIR saves a JSON receipt and screenshots.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const artifact = process.env.CPA_UI_ARTIFACT_DIR;
if (artifact) fs.mkdirSync(artifact, {recursive: true});
const entities = [
    {id: 1, slug: 'company-a', name: 'Company A — Synthetic Electrical Construction and Services', active: true, status: 'active'},
    {id: 2, slug: 'company-b', name: 'Company B — Synthetic', active: false, status: 'active'},
];
const result = {scope: 'isolated Chromium, actual source scripts, synthetic API responses; no live service', checks: [], requests: [], errors: []};
const passed = name => result.checks.push(name);

(async () => {
    const browser = await chromium.launch({headless: true});
    try {
        const context = await browser.newContext({viewport: {width: 1464, height: 950}});
        let denySecond = false;
        let releaseSlow;
        let releaseSave;
        await context.route('**/*', async route => {
            const req = route.request();
            const url = new URL(req.url());
            if (url.pathname.startsWith('/api/')) {
                result.requests.push({path: url.pathname, method: req.method(), entity: req.headers()['x-ark-entity'] || url.searchParams.get('_ark_entity'), principal: req.headers()['x-ark-principal'] || url.searchParams.get('_ark_principal')});
                if (url.pathname === '/api/slow') await new Promise(resolve => { releaseSlow = resolve; });
                if (url.pathname === '/api/slow-save') await new Promise(resolve => { releaseSave = resolve; });
                let value = [];
                if (url.pathname === '/api/ark-cpa/ui-context') value = {entity_mode: true, username: 'synthetic-reader'};
                if (url.pathname === '/api/ark-cpa/entities') value = denySecond ? entities.slice(0, 1) : entities;
                if (url.pathname === '/api/customers' && req.method() === 'POST') value = {entity: req.headers()['x-ark-entity'], name: req.postDataJSON().name};
                if (url.pathname === '/api/ark-cpa/recovery-status') value = {configured: true, local: {status: 'verified', verified_at: '2026-10-13T07:00:00Z'}, offsite: {status: 'not_verified', verified_at: null}};
                return route.fulfill({json: value});
            }
            if (url.pathname === '/static/js/auth.js') return route.fulfill({body: '', contentType: 'application/javascript'});
            const file = url.pathname === '/' ? path.join(root, 'index.html') : path.join(root, 'app', url.pathname);
            if (!file.startsWith(root + '/') || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({status: 404, body: 'isolated fixture'});
            if (url.pathname === '/static/js/app.js') return route.fulfill({body: fs.readFileSync(file, 'utf8').replace("document.addEventListener('DOMContentLoaded', () => App.init());", ''), contentType: 'application/javascript'});
            return route.fulfill({path: file});
        });
        async function prepare(page) {
            page.on('pageerror', e => result.errors.push(e.message));
            await page.goto('http://cpa.test/');
            await init(page);
        }
        async function init(page) {
            await page.evaluate(async () => {
                document.body.classList.remove('auth-pending');
                document.querySelector('#splash').classList.add('hidden');
                document.querySelector('#app').removeAttribute('inert');
                document.querySelector('#app').removeAttribute('aria-hidden');
                const assertInit = await ArkCPA.init({user: {role: 'readonly'}});
                if (!assertInit) throw new Error('Initialization failed');
                document.querySelector('#topbar-company').textContent = ArkCPA.activeEntity.name;
            });
        }
        const first = await context.newPage();
        const second = await context.newPage();
        await prepare(first);
        await prepare(second);
        await first.evaluate(() => openModal('Synthetic customer', `<form onsubmit="event.preventDefault(); API.post('/customers', {name: this.elements.name.value}).then(value => window.savedCustomer = value)"><label>Name<input name="name"></label><button type="submit">Save synthetic customer</button></form>`));
        await first.getByLabel('Name', {exact: true}).fill('Stays in company A');
        await Promise.all([second.waitForEvent('load'), second.selectOption('#ark-entity-select', 'company-b')]);
        await init(second);
        assert.equal(await second.locator('#ark-entity-select').inputValue(), 'company-b');
        // Also reproduce the exact previous shared-localStorage trigger.
        await second.evaluate(() => localStorage.setItem('arkcpa_entity', 'company-b'));
        await first.getByRole('button', {name: 'Save synthetic customer'}).click();
        await first.waitForFunction(() => window.savedCustomer);
        assert.deepEqual(await first.evaluate(() => window.savedCustomer), {entity: 'company-a', name: 'Stays in company A'});
        assert.equal(await first.locator('#ark-entity-select').inputValue(), 'company-a');
        passed('Two tabs and shared legacy localStorage cannot redirect an existing form');
        assert.equal(result.requests.filter(r => r.path.endsWith('/activate')).length, 0);
        passed('Readonly company selection requires no activation write');

        await first.evaluate(async () => { closeModal(); const fd = new FormData(); fd.set('file', new Blob(['synthetic']), 'synthetic.txt'); await fetch('/api/attachments/invoice/1', {method: 'POST', body: fd}); });
        assert.equal(result.requests.at(-1).entity, 'company-a');
        passed('Direct multipart upload retains tab context');
        await first.evaluate(() => { document.querySelector('#page-content').innerHTML = '<a href="/api/reports/example/pdf" target="_blank">Synthetic PDF</a>'; API.bindDOM(document.querySelector('#page-content')); });
        const href = await first.getByText('Synthetic PDF').getAttribute('href');
        assert.equal(new URL(href).searchParams.get('_ark_entity'), 'company-a');
        const popupPromise = context.waitForEvent('page');
        await first.evaluate(() => window.open('/api/reports/example/pdf', '_blank'));
        const popup = await popupPromise;
        await popup.waitForLoadState();
        assert.equal(new URL(popup.url()).searchParams.get('_ark_entity'), 'company-a');
        await popup.close();
        passed('Links and window.open scope native downloads to the displayed company');

        // Dirty forms retain their company when a switch is declined.
        await first.evaluate(() => { document.querySelector('#page-content').innerHTML = '<form><label>Unsaved name<input name="name"></label></form>'; API.bindDOM(document.querySelector('#page-content')); });
        await first.getByLabel('Unsaved name').fill('Keep my draft');
        first.once('dialog', d => d.dismiss());
        await first.selectOption('#ark-entity-select', 'company-b');
        await first.waitForFunction(() => !document.querySelector('#ark-entity-select').disabled);
        assert.equal(await first.locator('#ark-entity-select').inputValue(), 'company-a');
        assert.equal(await first.getByLabel('Unsaved name').inputValue(), 'Keep my draft');
        passed('Declined dirty-form switch preserves draft and selector');
        denySecond = true;
        await first.selectOption('#ark-entity-select', 'company-b');
        await first.waitForFunction(() => !document.querySelector('#ark-entity-select').disabled);
        assert.equal(await first.locator('#ark-entity-select').inputValue(), 'company-a');
        assert.equal(await first.getByLabel('Unsaved name').inputValue(), 'Keep my draft');
        denySecond = false;
        passed('Revoked membership resets selector without discarding draft');

        await first.evaluate(() => { window.pendingSave = API.post('/slow-save', {synthetic: true}); });
        await first.waitForFunction(() => API.pendingWrites === 1);
        await first.selectOption('#ark-entity-select', 'company-b');
        await first.waitForFunction(() => !document.querySelector('#ark-entity-select').disabled);
        assert.equal(await first.locator('#ark-entity-select').inputValue(), 'company-a');
        assert.ok(releaseSave);
        releaseSave();
        await first.evaluate(() => window.pendingSave);
        passed('Company switch waits for an in-flight save');

        await first.evaluate(() => { openModal('Stale form', '<form onsubmit="event.preventDefault(); window.staleSubmitted = true"><button type="submit">Submit stale form</button></form>'); API.setContext('company-b'); });
        await first.getByRole('button', {name: 'Submit stale form'}).click();
        assert.equal(await first.evaluate(() => window.staleSubmitted === true), false);
        await first.evaluate(() => { API.setContext('company-a'); closeModal(); });
        passed('A form bound to a previous company cannot submit');

        await first.evaluate(() => { closeModal(); window.slowResult = API.get('/slow').then(() => 'accepted', e => e.staleContext ? 'stale' : e.message); });
        await first.waitForTimeout(30);
        assert.ok(releaseSlow);
        await first.evaluate(() => API.setContext('company-b'));
        releaseSlow();
        assert.equal(await first.evaluate(() => window.slowResult), 'stale');
        await first.evaluate(() => API.setContext('company-a'));
        passed('Late response is rejected after context changes');
        await first.evaluate(async () => {
            App.routes['/slow-test'] = {label: 'Slow', page: 'slow', render: () => new Promise(resolve => window.releaseRender = resolve)};
            App.routes['/fast-test'] = {label: 'Fast', page: 'fast', render: async () => '<p>Current fast page</p>'};
            window.slowNavigation = App.navigate('#/slow-test');
            await App.navigate('#/fast-test');
            window.releaseRender('<p>Obsolete page</p>');
            await window.slowNavigation;
        });
        assert.equal(await first.locator('#page-content').innerText(), 'Current fast page');
        passed('Older navigation cannot overwrite the current page');

        await first.evaluate(async () => { document.querySelector('#page-content').innerHTML = await CompaniesPage.render(); });
        assert.match(await first.locator('#page-content').innerText(), /selector at the top/);
        assert.equal(await first.getByRole('button', {name: '+ New Company'}).count(), 0);
        passed('Governed Company Files routes to entity management');

        await first.evaluate(async () => { document.querySelector('#page-content').innerHTML = await ReportsPage.render(); API.bindDOM(document.querySelector('#page-content')); window.reportActivations = 0; ReportsPage.profitLoss = () => { window.reportActivations++; openModal('Keyboard report', '<button>Close report</button>'); }; });
        const card = first.getByRole('button', {name: /Profit & Loss Income/});
        await card.focus();
        await first.keyboard.press('Enter');
        assert.equal(await first.evaluate(() => window.reportActivations), 1);
        await first.evaluate(() => closeModal());
        assert.equal(await card.evaluate(el => el === document.activeElement), true);
        await first.keyboard.press('Space');
        assert.equal(await first.evaluate(() => window.reportActivations), 2);
        await first.evaluate(() => closeModal());
        assert.equal(await first.locator('.report-card').count(), 17);
        await first.evaluate(() => { document.querySelector('#page-content').innerHTML = ReportsPage._nonprofitCards(); });
        assert.equal(await first.locator('.report-card').count(), 6);
        passed('Business and nonprofit report cards are semantic buttons; Enter, Space and focus return work');

        await first.evaluate(async () => { document.querySelector('#page-content').innerHTML = '<div id="backup-list"></div><div id="recovery-status"></div>'; ArkCPA.userRole = 'admin'; await SettingsPage.loadBackups(); await SettingsPage.loadRecoveryStatus(); });
        assert.match(await first.locator('#backup-list').innerText(), /No application backups/);
        assert.match(await first.locator('#recovery-status').innerText(), /not been verified offsite/);
        passed('Empty application backup list does not imply disaster-recovery status');

        await first.evaluate(() => document.querySelector('#toast-container').replaceChildren());
        for (const width of [320, 390, 768, 1024, 1464, 1920, 732]) {
            await first.setViewportSize({width, height: 950});
            await first.waitForTimeout(220); // let the responsive drawer transition settle
            const bounds = await first.evaluate(() => {
                const box = id => { const r = document.getElementById(id).getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height}; };
                return {width: innerWidth, scroll: document.documentElement.scrollWidth, select: box('ark-entity-select'), logout: box('logout-btn')};
            });
            assert.ok(bounds.scroll <= width, JSON.stringify(bounds));
            for (const key of ['select', 'logout']) assert.ok(bounds[key].left >= 0 && bounds[key].right <= width && bounds[key].width > 0 && bounds[key].height > 0, JSON.stringify(bounds));
            assert.ok(bounds.select.right <= bounds.logout.left, JSON.stringify(bounds));
            if (artifact && [320, 1464].includes(width)) await first.screenshot({path: path.join(artifact, `header-${width}.png`)});
        }
        passed('Header, company selector and Sign Out remain within 320–1920px and 732px (1464px at 200% equivalent layout)');
        assert.deepEqual(result.errors, []);
        passed('No uncaught browser errors');
        await context.close();
    } finally {
        await browser.close();
        if (artifact) fs.writeFileSync(path.join(artifact, 'browser-result.json'), JSON.stringify(result, null, 2));
    }
    console.log(JSON.stringify({scope: result.scope, checks: result.checks, errors: result.errors}, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
