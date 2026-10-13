/* Actual Analytics helpers, native Chromium downloads and a real HTTP server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const requests = [];
const errors = [];
const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname.startsWith('/api/analytics/export.')) {
        requests.push({url: req.url, cookie: req.headers.cookie});
        res.writeHead(200, {'Content-Type': 'application/octet-stream',
            'Content-Disposition': 'attachment; filename="synthetic-export.txt"'});
        res.end('synthetic only');
    } else if (['/api.js', '/analytics.js'].includes(url.pathname)) {
        res.writeHead(200, {'Content-Type': 'application/javascript'});
        res.end(fs.readFileSync(path.join(root, 'app/static/js', url.pathname.slice(1))));
    } else if (url.pathname === '/') {
        res.writeHead(200, {'Content-Type': 'text/html'});
        res.end(`<script>function toast() {}</script>
            <script src="/api.js"></script><script src="/analytics.js"></script>
            <button onclick="AnalyticsPage._download('csv')">Export CSV</button>
            <button onclick="AnalyticsPage._download('pdf')">Export PDF</button>
            <div data-ark-entity="company-b" data-ark-principal="synthetic-reader">
            <a href="/api/analytics/export.csv">Stale company link</a></div>
            <script>API.setContext('company-a', 'synthetic-reader');
            document.cookie = 'synthetic_active_entity=company-b';</script>`);
    } else {
        res.writeHead(404); res.end();
    }
});

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({headless: true});
    try {
        const page = await browser.newPage();
        page.on('pageerror', error => errors.push(error.message));
        await page.goto(`http://127.0.0.1:${server.address().port}/`);
        for (const format of ['CSV', 'PDF']) {
            const [download] = await Promise.all([
                page.waitForEvent('download'),
                page.getByRole('button', {name: `Export ${format}`}).click(),
            ]);
            assert.equal(await download.failure(), null);
            const request = requests.at(-1);
            const url = new URL(request.url, 'http://localhost');
            assert.equal(url.pathname, `/api/analytics/export.${format.toLowerCase()}`);
            assert.equal(url.searchParams.get('_ark_entity'), 'company-a');
            assert.equal(url.searchParams.get('_ark_principal'), 'synthetic-reader');
            assert.equal(request.cookie, 'synthetic_active_entity=company-b');
        }
        await page.getByRole('link', {name: 'Stale company link'}).click();
        await page.waitForTimeout(200);
        assert.equal(requests.length, 2, 'stale bound link must never make a request');
        assert.deepEqual(errors, []);
        const result = {scope: 'isolated real HTTP server and Chromium; actual Analytics helper',
            passed: true, checks: ['CSV bound to displayed company', 'PDF bound to displayed company',
                'Shared stale cookie cannot select export company', 'Stale DOM link denied', 'No page errors'], requests, errors};
        if (process.env.CPA_UI_ARTIFACT_DIR) {
            fs.mkdirSync(process.env.CPA_UI_ARTIFACT_DIR, {recursive: true});
            fs.writeFileSync(path.join(process.env.CPA_UI_ARTIFACT_DIR, 'native-exports.json'), JSON.stringify(result, null, 2));
        }
        console.log(JSON.stringify(result, null, 2));
    } finally {
        await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
