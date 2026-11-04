// Synthetic property API contracts. No real SVN, writes or repository access.
const {chromium}=require('playwright');const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');const path=require('node:path');
const server=spawn(process.env.PYTHON||'python3',[path.resolve(__dirname,'../server.py'),'--port','0']);
(async()=>{let browser;try{
 const url=await new Promise((resolve,reject)=>{let out='';server.stdout.on('data',chunk=>{out+=chunk;const m=out.match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});server.on('error',reject);server.on('exit',c=>reject(Error('Server exited '+c)));setTimeout(()=>reject(Error('Server startup timeout')),8000).unref();});
 browser=await chromium.launch({...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});const page=await browser.newPage({viewport:{width:1440,height:960}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const rows=[{id:'a',path:'one.txt',item:'modified',props:'modified'},{id:'b',path:'new.txt',item:'added',props:'none'}];
 const snapshot={mode:'live',workspace:'SYNTHETIC',root:'/synthetic',workspaceId:'w',selectionVersion:1,snapshotToken:'s',fileReadsEnabled:true,propertyReadsEnabled:true,rows};
 const json=(route,payload,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(payload)});let requests=[],fail=false;
 await page.route('**/api/config',route=>json(route,snapshot));await page.route('**/api/status',route=>json(route,snapshot));
 await page.route('**/api/file-properties',route=>{const body=route.request().postDataJSON();requests.push(body);if(fail)return json(route,{error:'Synthetic local read failed'},503);const added=body.id==='b';return json(route,{...body,path:added?'new.txt':'one.txt',mode:'live',localOnly:true,displayOnly:true,baseAvailable:!added,baseUnavailableReason:added?'added-file':null,properties:[{name:'test:review',change:added?'unbased':'added',base:null,working:{kind:'text',text:'<script>not executable</script>',byteLength:31,sha256:'a'.repeat(64),textTruncated:false,redacted:false,escapedControls:0}}]});});
 await page.goto(url);await page.locator('.file-row').first().click();await page.locator('[data-preview="properties"]').click();assert.equal(requests.length,0);
 await page.locator('#read-properties').click();await page.locator('.property-entry').waitFor();assert.equal(requests.length,1);assert.deepEqual(Object.keys(requests[0]).sort(),['id','selectionVersion','snapshotToken','workspaceId']);assert.match(await page.locator('.property-value-text').innerText(),/<script>not executable<\/script>/);
 await page.locator('.file-row').nth(1).click();assert.equal(await page.locator('.property-entry').count(),0);assert.equal(requests.length,1);await page.locator('#read-properties').click();await page.locator('.property-entry').waitFor();assert.match(await page.locator('#diff-content').innerText(),/新增文件，没有 BASE/);
 fail=true;await page.locator('#read-properties').click();await page.locator('.read-error').waitFor();assert.equal(await page.locator('.property-entry').count(),0);assert.match(await page.locator('.read-error').innerText(),/失败不代表没有属性/);
 await page.locator('[data-preview="diff"]').click();assert.equal(await page.locator('#read-properties').count(),0);assert.deepEqual(errors,[]);
 console.log('Property browser contract passed (synthetic): explicit request, exact scope, escaping, added BASE distinction and failure clearing');
 }finally{if(browser)await browser.close();server.kill();}})().catch(error=>{console.error(error);process.exitCode=1;});
