// Synthetic HTTP contract tests. No SVN writes or real backups are performed.
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const server=spawn(process.env.PYTHON||'python3',[path.resolve(__dirname,'../server.py'),'--port','0']);
(async()=>{let browser;try{
 const url=await new Promise((resolve,reject)=>{let output='';server.stdout.on('data',chunk=>{output+=chunk;const m=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});server.on('error',reject);server.on('exit',code=>reject(Error('Server exit '+code)));setTimeout(()=>reject(Error('Server startup timeout')),8000).unref();});
 browser=await chromium.launch({...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:960}}),errors=[];await page.clock.install();page.on('pageerror',e=>errors.push(e.message));
 const rows=[{id:'one',path:'visible/a <x>.txt',item:'modified',props:'none',revision:8,kind:'file'},{id:'two',path:'hidden/b.txt',item:'unversioned',props:'none',revision:null,kind:'file'}];
 let snapshot={mode:'live',workspace:'SYNTHETIC',root:'/synthetic',workspaceId:'w',selectionVersion:1,snapshotToken:'s1',shelfWritesEnabled:false,rows};
 const files=rows.map(r=>({path:r.path,state:r.item,kind:'file',size:2}));const shelf={id:'saved',name:'saved backup',createdAt:'2026-11-03T20:00:00Z',format:2,entryCount:2,files,totalBytes:4};
 const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 await page.route('**/api/config',r=>json(r,snapshot));await page.route('**/api/status',r=>json(r,snapshot));
 let lists=0,preflights=[],applies=[],lastPlan,preflightGate=null,applyGate=null,mismatch=false,fail=false;
 await page.route('**/api/shelves-list',r=>{lists++;return json(r,{...snapshot,shelves:[shelf,{id:'broken',name:'bad backup',error:'Invalid manifest'}]});});
 await page.route('**/api/shelf-preflight',async r=>{const b=r.request().postDataJSON();preflights.push(b);lastPlan=b;const result={...b,mode:'live',name:b.name||shelf.name,localOnly:true,backupOnly:b.operation==='create',planToken:'p'+preflights.length,expiresIn:120,files:files.map(f=>({...f})),totalBytes:4};if(mismatch)result.files[0].path='wrong';if(preflightGate)await preflightGate;await json(r,result);});
 await page.route('**/api/shelf-apply',async r=>{const b=r.request().postDataJSON();applies.push(b);if(applyGate)await applyGate;if(fail)return json(r,{error:'Synthetic partial restore failure'},503);snapshot={...snapshot,snapshotToken:'s'+(applies.length+1)};await json(r,{...snapshot,statusRefreshed:true,shelfResult:{outcome:'verified',verified:true,warnings:[],operation:lastPlan.operation,name:lastPlan.name||shelf.name,shelfId:'saved',files:files.map(f=>f.path),shelfRetained:true}});});
 const ready=()=>page.locator('#shelf-confirm:not([disabled])').waitFor();
 const open=()=>page.locator('#create-shelf').click();const preflight=async()=>{await page.locator('#shelf-preflight').click();await ready();};
 await page.goto(url);await page.locator('.file-row').first().click();assert(await page.locator('#create-shelf').isDisabled());assert(await page.locator('#browse-shelves').isDisabled());assert.equal(lists,0);
 snapshot={...snapshot,shelfWritesEnabled:true};await page.locator('#refresh').click();await page.locator('#create-shelf:not([disabled])').waitFor();await page.locator('#select-visible').check();await page.locator('#search').fill('visible/');
 await open();assert.equal(await page.locator('#shelf-files li').count(),2);assert.match(await page.locator('#shelf-files').innerText(),/hidden\/b.txt/);assert.equal(await page.locator('#shelf-files x').count(),0);
 await page.locator('#shelf-name').fill('backup');await preflight();assert.deepEqual(preflights.at(-1).ids,['one','two']);assert.equal(applies.length,0);
 if(process.env.QA_SCREENSHOT_PATH)await page.screenshot({path:process.env.QA_SCREENSHOT_PATH,fullPage:true});
 await page.locator('#shelf-name').fill('changed');assert(await page.locator('#shelf-confirm').isDisabled());await preflight();await page.clock.fastForward(120001);assert(await page.locator('#shelf-confirm').isDisabled());await preflight();
 await page.locator('#shelf-confirm').click();await page.locator('#shelf-result').filter({hasText:'已创建'}).waitFor();assert.equal(applies.length,1);assert.deepEqual(Object.keys(applies[0]).sort(),['confirmed','planToken','selectionVersion','snapshotToken','workspaceId']);await page.locator('#modal-done').click();
 // Full restore uses the shelf, never current filtered selection or a subset.
 assert.equal(lists,0);await page.locator('#browse-shelves').click();await page.locator('[data-shelf-index="0"]').click();assert.equal(lists,1);assert(await page.locator('[data-shelf-index="1"]').isDisabled());assert.equal(await page.locator('#shelf-files li').count(),2);await preflight();assert.equal(preflights.at(-1).operation,'restore');assert.equal(preflights.at(-1).ids,undefined);await page.locator('#shelf-confirm').click();await page.locator('#shelf-result').filter({hasText:'已恢复'}).waitFor();await page.locator('#modal-done').click();
 // Mismatched scope is rejected; closing a pending preflight discards response.
 await open();await page.locator('#shelf-name').fill('test');mismatch=true;await page.locator('#shelf-preflight').click();await page.locator('#shelf-result').filter({hasText:'预检失败'}).waitFor();assert(await page.locator('#shelf-confirm').isDisabled());mismatch=false;
 let release;preflightGate=new Promise(r=>release=r);await page.locator('#shelf-preflight').click();await page.waitForFunction(()=>document.querySelector('#shelf-preflight').disabled);await page.locator('#modal-close').click();release();preflightGate=null;await page.locator('#create-shelf:not([disabled])').waitFor();assert(await page.locator('#shelf-confirm').isDisabled());
 // Single-flight apply survives close: retain verified completion, reject stale rows.
 await open();await page.locator('#shelf-name').fill('test');await preflight();applyGate=new Promise(r=>release=r);const before=applies.length;await page.locator('#shelf-confirm').click();assert(await page.locator('#shelf-confirm').isDisabled());await page.locator('#modal-close').click();assert.match(await page.locator('#live-error').innerText(),/关闭不等于取消/);release();applyGate=null;await page.locator('#refresh:not([disabled])').waitFor();assert.equal(applies.length,before+1);assert.match(await page.locator('#shelf-last-result').innerText(),/本地 Shelf 已创建/);assert.match(await page.locator('#live-error').innerText(),/显式刷新/);
 // Server partial failure forces explicit refresh and does not automatically retry.
 await page.locator('#refresh').click();await page.locator('#browse-shelves:not([disabled])').waitFor();await page.locator('#browse-shelves').click();await page.locator('[data-shelf-index="0"]').click();await preflight();fail=true;await page.locator('#shelf-confirm').click();await page.locator('#shelf-result').filter({hasText:'结果未知'}).waitFor();assert(await page.locator('#shelf-confirm').isDisabled());assert.deepEqual(errors,[]);
 console.log('Shelf browser contracts passed');
 }finally{if(browser)await browser.close();server.kill();}})().catch(error=>{console.error(error);process.exitCode=1;});
