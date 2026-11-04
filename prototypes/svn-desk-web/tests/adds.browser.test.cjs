// Synthetic HTTP contracts only. Never real repository or working-copy writes.
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const path=require('node:path');const assert=require('node:assert/strict');
const server=spawn(process.env.PYTHON||'python3',[path.resolve(__dirname,'../server.py'),'--port','0']);
(async()=>{let browser;try{
 const url=await new Promise((resolve,reject)=>{let out='';server.stdout.on('data',c=>{out+=c;const m=out.match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});server.on('error',reject);server.on('exit',c=>reject(Error('Server exit '+c)));setTimeout(()=>reject(Error('Startup timeout')),8000).unref();});
 browser=await chromium.launch({...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});const page=await browser.newPage({viewport:{width:1600,height:1100}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const rows=[{id:'a',path:'visible/a',item:'unversioned',props:'none',kind:'file'},{id:'b',path:'hidden/<b>',item:'unversioned',props:'none',kind:'file'}];
 let snapshot={mode:'live',workspace:'SYNTHETIC',root:'/synthetic',workspaceId:'w',selectionVersion:1,snapshotToken:'s',addsEnabled:false,rows};
 const json=(r,d,status=200)=>r.fulfill({status,contentType:'application/json',body:JSON.stringify(d)});const preflights=[],applies=[];
 await page.route('**/api/config',r=>json(r,snapshot));await page.route('**/api/status',r=>json(r,snapshot));
 await page.route('**/api/add-preflight',r=>{const request=r.request().postDataJSON();preflights.push(request);return json(r,{...request,mode:'live',planToken:'p',expiresIn:120,localOnly:true,autoProps:false,files:rows.filter(f=>request.ids.includes(f.id)).map(({id,path})=>({id,path}))});});
 await page.route('**/api/add-apply',r=>{applies.push(r.request().postDataJSON());return json(r,{...snapshot,rows:[],snapshotToken:null,statusRefreshed:false,addResult:{action:'add',files:rows.map(f=>f.path),outcome:'verified',verified:true,autoProps:false,warnings:['status-refresh-failed']}});});
 await page.goto(url);await page.locator('.file-row').first().click();assert(await page.locator('#add-selected').isDisabled());
 snapshot.addsEnabled=true;await page.locator('#refresh').click();await page.locator('#add-selected:not([disabled])').waitFor();
 await page.locator('.file-row').nth(1).click({modifiers:['Control']});await page.locator('#search').fill('visible/');
 await page.locator('#add-selected').click();assert.match(await page.locator('#add-files').innerText(),/hidden\/<b>/);assert.equal(preflights.length,0);assert(await page.locator('#add-confirm').isDisabled());
 await page.locator('#add-preflight').click();await page.locator('#add-confirm:not([disabled])').waitFor();assert.equal(applies.length,0);assert.deepEqual(preflights[0].ids,['a','b']);
 await page.keyboard.press('Escape');assert.equal(await page.locator('#modal').isVisible(),false);
 await page.locator('#add-selected').click();assert(await page.locator('#add-confirm').isDisabled());
 await page.locator('#add-preflight').click();await page.locator('#add-confirm:not([disabled])').waitFor();await page.locator('#add-confirm').click();
 await page.locator('#add-last-result').filter({hasText:'已核实添加 2 个文件'}).waitFor();await page.locator('#modal-done').click();
 assert.match(await page.locator('#add-last-result').innerText(),/尚未提交/);assert.equal(applies.length,1);assert.equal(await page.locator('.file-row').count(),0);assert(await page.locator('#add-selected').isDisabled());assert.deepEqual(errors,[]);
 console.log('Add browser contracts passed: explicit hidden selection, separate confirmation, dismiss/reopen, verified result retained');
 }finally{if(browser)await browser.close();server.kill();}})().catch(e=>{console.error(e);process.exitCode=1;});
