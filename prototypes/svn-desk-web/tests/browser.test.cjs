// Optional reproducible browser smoke tests. Requires Playwright + a browser.
// This script was NOT run in the current test environment; actual manual browser interaction
// coverage is separately documented in docs/VALIDATION.md.
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs/promises');
const root=path.resolve(__dirname,'..');
const server=spawn(process.env.PYTHON||'python3',[path.join(root,'server.py'),'--port','0']);
(async()=>{
 let browser;
 try{
  const url=await new Promise((resolve,reject)=>{let text='';server.stdout.on('data',chunk=>{text+=chunk;const m=text.match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});server.on('error',reject);server.on('exit',code=>reject(new Error('Server stopped: '+code)));setTimeout(()=>reject(new Error('Server startup timeout')),8000).unref();});
  browser=await chromium.launch({...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:960}});
  const appErrors=[];page.on('pageerror',e=>appErrors.push(e.message));
  await page.goto(url);await page.locator('.file-row').first().waitFor();
  assert.equal(await page.locator('.file-row').count(),8);
  await page.locator('#search').fill('Forest');assert.equal(await page.locator('.file-row').count(),1);
  assert.match(await page.locator('#selection-summary').innerText(),/含 1 项不可见/);
  await page.locator('#review').click();assert.match(await page.locator('#modal-content').innerText(),/PlayerController.cs/);assert.equal(await page.locator('.review-file').count(),1);
  await page.locator('#modal-done').click();await page.getByLabel('选择 Assets/Scenes/Forest.unity',{exact:true}).check();
  await page.locator('#review').click();assert.equal(await page.locator('.review-file').count(),2);await page.keyboard.press('Escape');assert.equal(await page.locator('#modal').isVisible(),false);
  await page.locator('#clear-filters').click();await page.locator('#collapse-all').click();assert.equal(await page.locator('.file-row').count(),0);await page.locator('#collapse-all').click();assert.equal(await page.locator('.file-row').count(),12);
  await page.locator('#search').fill('no-match');assert.equal(await page.locator('#empty-state').isVisible(),true);await page.locator('#empty-reset').click();
  await page.getByRole('button',{name:'筛选目录 Assets/Scripts/Player',exact:true}).click();assert.equal(await page.locator('.file-row').count(),3);
  await page.getByRole('row',{name:'PlayerController.cs，已修改',exact:true}).click();await page.locator('#history-tab').click();assert.match(await page.locator('#modal-content').innerText(),/虚构历史/);await page.locator('#modal-done').click();
  // Simulated live recovery: explicitly synthetic metadata, not a real SVN test.
  const fixture={mode:'live',workspace:'TEST FIXTURE',root:'/fixture/svn-wc',rows:[{id:'a.cs',path:'a.cs',item:'normal',props:'modified',revision:1,kind:'unknown'}]};
  let configCalls=0;
  await page.route('**/api/config',route=>{configCalls++;return route.fulfill({status:configCalls===1?503:200,contentType:'application/json',body:JSON.stringify({mode:'live',workspace:fixture.workspace,root:fixture.root,writesEnabled:false})});});
  await page.route('**/api/status',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(fixture)}));
  await page.reload();await page.locator('#live-error:not(.hidden)').waitFor();assert.equal(await page.locator('.file-row').count(),0);
  await page.locator('#refresh').click();await page.locator('.file-row').first().waitFor();assert.equal(await page.locator('#workspace-name').innerText(),'TEST FIXTURE');assert.equal(await page.locator('#workspace-address').innerText(),'/fixture/svn-wc');
  await page.locator('[data-filter="modified"]').click();assert.equal(await page.locator('.file-row').count(),1);
  await page.unroute('**/api/status');await page.route('**/api/status',route=>route.fulfill({status:503,body:'{}'}));await page.locator('#refresh').click();await page.locator('#live-error:not(.hidden)').waitFor();assert.equal(await page.locator('.file-row').count(),0);assert.match(await page.locator('#mode-pill').innerText(),/状态未知/);
  assert.deepEqual(appErrors,[]);
  console.log('PASS browser smoke, selection boundaries, synthetic config recovery, failure-closed state');
 }finally{if(browser)await browser.close();server.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
