/* Actual authority panel/handlers in Chromium; synthetic HTTP responses only. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'../..');
const artifact=process.env.CPA_UI_ARTIFACT_DIR;
const result={scope:'isolated Chromium, actual authority panel source, synthetic HTTP',checks:[],errors:[]};
(async()=>{
    const browser=await chromium.launch({headless:true});
    try {
        const page=await browser.newPage();
        let canRefresh=false;
        let denied=false;
        let posts=0;
        page.on('pageerror',e=>result.errors.push(e.message));
        await page.route('**/*',async route=>{
            const url=new URL(route.request().url());
            if(url.pathname==='/') return route.fulfill({contentType:'text/html',body:'<!doctype html><div id="page-content"></div><div id="receipt" role="status"></div><script src="/static/js/arkcpa.js"></script>'});
            if(url.pathname==='/static/js/arkcpa.js') return route.fulfill({contentType:'application/javascript',path:path.join(root,'app/static/js/arkcpa.js')});
            if(url.pathname==='/api/ark-cpa/authority-sources/refresh') {
                posts++;
                return denied ? route.fulfill({status:403,json:{detail:'Explicit global research permission required'}}) : route.fulfill({json:{results:[{status:'failed'},{status:'review_required'}]}});
            }
            if(url.pathname==='/api/ark-cpa/authority-sources') return route.fulfill({json:{can_refresh:canRefresh,sources:[{code:'SYNTHETIC',title:'Synthetic Reference',url:'https://www.irs.gov/synthetic',jurisdiction:'United States / Texas',current_hash:'a'.repeat(64)}],proposals:[]}});
            return route.fulfill({json:[]});
        });
        await page.goto('http://cpa.test/');
        await page.evaluate(async()=>{
            window.escapeHtml=value=>{const d=document.createElement('div');d.textContent=value;return d.innerHTML;};
            window.toast=(message,type)=>{document.querySelector('#receipt').textContent=message;document.querySelector('#receipt').dataset.type=type;};
            window.API={get:async path=>(await fetch('/api'+path)).json(),post:async(path,body)=>{const r=await fetch('/api'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const value=await r.json();if(!r.ok)throw Error(value.detail);return value;}};
            window.paint=async()=>{document.querySelector('#page-content').innerHTML=await ArkCPA.renderCompliance();};
            window.App={navigate:()=>window.paint()};
            ArkCPA.activeEntity={id:1,jurisdiction:'United States / Texas',protected_approver:true};
            await window.paint();
        });
        assert.equal(await page.getByRole('button',{name:'Check registered sources'}).count(),0);
        assert.equal(posts,0);
        assert.ok((await page.locator('#page-content').textContent()).includes('Review and applicability required'));
        assert.ok(!(await page.locator('#page-content').textContent()).includes('aaaaaaaaaa'));
        result.checks.push('Company approver alone sees no global refresh; legacy current hash is not approval');
        canRefresh=true;
        await page.evaluate(()=>window.paint());
        await page.getByRole('button',{name:'Check registered sources'}).click();
        await page.waitForFunction(()=>document.querySelector('#receipt').textContent.includes('1 failed or blocked'));
        assert.equal(posts,1);
        assert.equal(await page.locator('#receipt').getAttribute('data-type'),'error');
        result.checks.push('Refresh click reports failures and pending review truthfully');
        denied=true;
        await page.evaluate(()=>window.paint());
        await page.getByRole('button',{name:'Check registered sources'}).click();
        await page.waitForFunction(()=>document.querySelector('#receipt').textContent.includes('Explicit global research'));
        assert.equal(await page.getByRole('button',{name:'Check registered sources'}).isEnabled(),true);
        result.checks.push('Permission revocation error restores usable control');
        assert.deepEqual(result.errors,[]);
        if(artifact){fs.mkdirSync(artifact,{recursive:true});fs.writeFileSync(path.join(artifact,'browser-authority.json'),JSON.stringify(result,null,2));}
        console.log(JSON.stringify(result,null,2));
    } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
