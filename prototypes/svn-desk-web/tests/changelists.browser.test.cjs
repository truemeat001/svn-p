// Browser contract tests: synthetic native status and write responses, never invokes SVN.
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const server=spawn(process.env.PYTHON||'python3',[path.resolve(__dirname,'../server.py'),'--port','0']);
(async()=>{let browser;try{
 const url=await new Promise((resolve,reject)=>{let output='';server.stdout.on('data',chunk=>{output+=chunk;const m=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});server.on('error',reject);server.on('exit',code=>reject(Error('Server exit '+code)));setTimeout(()=>reject(Error('Server startup timeout')),8000).unref();});
 browser=await chromium.launch({...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:960}}),errors=[];await page.clock.install();page.on('pageerror',e=>errors.push(e.message));
 const rows=[{id:'one',path:'visible/a <x>.txt',changelist:'old',item:'modified',props:'none',revision:8,kind:'file'},{id:'two',path:'hidden/b.txt',changelist:'old',item:'modified',props:'none',revision:8,kind:'file'}];
 let snapshot={mode:'live',workspace:'SYNTHETIC',root:'/synthetic',workspaceId:'a',selectionVersion:1,snapshotToken:'s1',fileReadsEnabled:true,changelistWritesEnabled:false,rows};
 const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 await page.route('**/api/config',r=>json(r,snapshot));await page.route('**/api/status',r=>json(r,snapshot));
 let preflights=[],applies=[],gate=null,applyGate=null,mismatch=false,fail=false,lastPlan;
 await page.route('**/api/changelist-preflight',async route=>{const b=route.request().postDataJSON();preflights.push(b);lastPlan=b;const result={...b,mode:'live',planToken:'plan'+preflights.length,expiresIn:120,localOnly:true,files:b.ids.map(id=>{const row=snapshot.rows.find(r=>r.id===id);return {id,path:row.path,fromChangelist:row.changelist||null};})};if(mismatch)result.files[0].path='wrong';if(gate)await gate;await json(route,result);});
 await page.route('**/api/changelist-apply',async route=>{const b=route.request().postDataJSON();applies.push(b);if(applyGate)await applyGate;if(fail)return json(route,{error:'synthetic partial failure'},503);snapshot={...snapshot,snapshotToken:'s'+(applies.length+1),rows:snapshot.rows.map(r=>lastPlan.ids.includes(r.id)?{...r,changelist:lastPlan.name}:r)};await json(route,{...snapshot,changelistResult:{action:lastPlan.action,name:lastPlan.name,count:lastPlan.ids.length}});});
 const ready=()=>page.locator('#changelist-confirm:not([disabled])').waitFor();
 const open=async()=>{await page.locator('#move-changelist').click();};
 const preflight=async name=>{if(name!==undefined)await page.locator('#changelist-name').fill(name);await page.locator('#changelist-preflight').click();await ready();};
 await page.goto(url);await page.locator('.file-row').first().click();assert.equal(await page.locator('#move-changelist').isDisabled(),true);
 snapshot={...snapshot,changelistWritesEnabled:true};await page.locator('#refresh').click();await page.locator('#move-changelist:not([disabled])').waitFor();
 await page.locator('#select-visible').check();await page.locator('#search').fill('visible/');assert.match(await page.locator('#selection-summary').innerText(),/含 1 项不可见/);
 await open();assert.equal(await page.locator('#changelist-files li').count(),2);assert.match(await page.locator('#changelist-files').innerText(),/hidden\/b.txt/);assert.equal(await page.locator('#changelist-files x').count(),0);
 await preflight('  New <标签>  ');if(process.env.QA_SCREENSHOT_PATH)await page.screenshot({path:process.env.QA_SCREENSHOT_PATH,fullPage:true});assert.equal(applies.length,0);assert.deepEqual(preflights.at(-1).ids,['one','two']);assert.equal(preflights.at(-1).name,'New <标签>');
 await page.locator('#changelist-name').fill('edited');assert.equal(await page.locator('#changelist-confirm').isDisabled(),true);await preflight();
 await page.locator('#changelist-confirm').click();await page.locator('#changelist-result').filter({hasText:'修改完成'}).waitFor();assert.equal(applies.length,1);assert.equal(applies[0].confirmed,true);assert.deepEqual(Object.keys(applies[0]).sort(),['confirmed','planToken','selectionVersion','snapshotToken','workspaceId']);
 await page.locator('#modal-done').click();await page.locator('#search').fill('');assert.equal(await page.locator('.group-label').filter({hasText:'old'}).count(),0);
 // Remove-to-default is a separate explicit plan/action, not an empty name.
 await open();await page.locator('#changelist-action').selectOption('remove');await preflight();assert.equal(preflights.at(-1).name,null);await page.locator('#changelist-confirm').click();await page.locator('#changelist-result').filter({hasText:'修改完成'}).waitFor();await page.locator('#modal-done').click();assert.equal(await page.locator('.group-label').innerText(),'默认变更列表');
 // Rejected plan, input validation, and delayed edit never enable confirmation.
 await open();await page.locator('#changelist-name').fill(' ');const count=preflights.length;await page.locator('#changelist-preflight').click();assert.equal(preflights.length,count);
 mismatch=true;await page.locator('#changelist-name').fill('new');await page.locator('#changelist-preflight').click();await page.locator('#changelist-result').filter({hasText:'不匹配'}).waitFor();assert.equal(await page.locator('#changelist-confirm').isDisabled(),true);mismatch=false;
 let release;gate=new Promise(resolve=>release=resolve);await page.locator('#changelist-preflight').click();await page.locator('#changelist-name').fill('newer');release();await page.locator('#changelist-preflight:not([disabled])').waitFor();gate=null;assert.equal(await page.locator('#changelist-confirm').isDisabled(),true);
 // Close/Esc discards late plan and never reopens.
 gate=new Promise(resolve=>release=resolve);await page.locator('#changelist-preflight').click();await page.keyboard.press('Escape');release();await page.waitForTimeout(70);gate=null;assert.equal(await page.locator('#modal').isVisible(),false);
 // Refresh invalidates a ready plan. No write is sent.
 await open();await preflight('fresh');await page.keyboard.press('F5');await page.waitForTimeout(70);assert.equal(await page.locator('#changelist-confirm').isDisabled(),true);await page.keyboard.press('Escape');
 // Selection changes invalidate even without a focus change.
 await open();await preflight('fresh');await page.evaluate(()=>document.querySelector('.file-row input').click());assert.equal(await page.locator('#changelist-confirm').isDisabled(),true);await page.keyboard.press('Escape');await page.locator('#select-visible').check();
 // A two-minute plan expires locally and cannot be reused.
 await open();await preflight('expiry');await page.clock.fastForward(120001);assert.equal(await page.locator('#changelist-confirm').isDisabled(),true);assert.match(await page.locator('#changelist-result').innerText(),/已过期/);await page.keyboard.press('Escape');
 // Apply runs once; closing is not cancellation, status clears until fresh result.
 await open();await preflight('closed-result');applyGate=new Promise(resolve=>release=resolve);const before=applies.length;await page.locator('#changelist-confirm').click();await page.evaluate(()=>document.getElementById('changelist-confirm').click());await page.locator('#modal-done').click();await page.locator('#live-error').filter({hasText:'不等于取消'}).waitFor();assert.equal(await page.locator('.file-row').count(),0);release();await page.locator('#live-error').waitFor({state:'hidden'});applyGate=null;assert.equal(applies.length,before+1);assert.equal(await page.locator('#modal').isVisible(),false);assert.equal(await page.locator('.file-row').count(),2);
 // Unknown/partial failure clears prior snapshot and never auto retries.
 await page.locator('#select-visible').check();await open();await preflight('failure');fail=true;await page.locator('#changelist-confirm').click();await page.locator('#changelist-result').filter({hasText:'可能已部分或全部生效'}).waitFor();assert.equal(await page.locator('.file-row').count(),0);const failedCount=applies.length;await page.waitForTimeout(60);assert.equal(applies.length,failedCount);assert.equal(await page.locator('#changelist-confirm').isDisabled(),true);await page.keyboard.press('Escape');fail=false;await page.locator('#refresh').click();await page.locator('.file-row').first().waitFor();
 // Demo uses identical two-phase confirmation with zero HTTP write requests.
 snapshot={mode:'demo',workspace:'Demo',root:'demo'};await page.reload();await page.locator('#move-changelist:not([disabled])').waitFor();const beforeDemo=[preflights.length,applies.length];await open();await preflight('Demo local');await page.locator('#changelist-confirm').click();await page.locator('#changelist-result').filter({hasText:'演示模拟完成'}).waitFor();assert.deepEqual([preflights.length,applies.length],beforeDemo);
 assert.deepEqual(errors,[]);console.log('PASS changelist browser contract flows: opt-in, hidden selection, assign/remove, invalidation, close while applying, unknown outcome, demo isolation.');
 }finally{await browser?.close();server.kill();}})().catch(error=>{console.error(error);process.exitCode=1;});
