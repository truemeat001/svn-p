// Optional real-browser acceptance. HTTP fixtures are synthetic, never a user's WC.
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const server=spawn(process.env.PYTHON||'python3',[path.join(__dirname,'..','server.py'),'--port','0']);
(async()=>{
 let browser;
 try{
  const url=await new Promise((resolve,reject)=>{let output='';server.stdout.on('data',data=>{output+=data;const m=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});server.on('error',reject);server.on('exit',code=>reject(Error(`Server exit ${code}`)));setTimeout(()=>reject(Error('Server startup timeout')),8000).unref();});
  browser=await chromium.launch({...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:960}}),errors=[],requests=[];
  page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{if(request.url().includes('/api/'))requests.push(request);});
  await page.goto(url);await page.locator('.file-row').first().waitFor();
  const menu=page.locator('#file-context-menu'),row=index=>page.locator(`[data-row-index="${index}"]`),action=id=>page.locator(`[data-file-action="${id}"]`);
  await row(1).locator('input').check();await page.locator('#search').fill('PlayerController');
  const before=requests.length;await row(0).click({button:'right'});assert.equal(await menu.isVisible(),true);
  assert.match(await menu.innerText(),/完整选区 2 项 · 含 1 项不可见/);assert.equal(requests.length,before);
  await action('review').click();assert.equal(await page.locator('.review-file').count(),2);await page.keyboard.press('Escape');
  await row(0).focus();await page.keyboard.press('Shift+F10');assert.equal(await menu.isVisible(),true);
  await page.keyboard.press('End');assert.equal(await action('changelist').evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('ArrowDown');assert.equal(await action('diff').evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('ArrowUp');assert.equal(await action('changelist').evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('Home');assert.equal(await action('diff').evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('Escape');assert.equal(await menu.isVisible(),false);assert.equal(await row(0).evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('ContextMenu');assert.equal(await menu.isVisible(),true);await page.keyboard.press('Tab');assert.equal(await menu.isVisible(),false);
  await row(0).click({button:'right'});await page.locator('#search').click();assert.equal(await menu.isVisible(),false);
  await page.locator('#clear-filters').click();await row(2).click({button:'right'});assert.match(await menu.innerText(),/完整选区 1 项/);
  await action('history').click();assert.match(await page.locator('#modal-content').innerText(),/没有向真实仓库查询/);await page.keyboard.press('Escape');
  assert.equal(requests.length,before,'opening, keyboard navigation and local dialogs do not send APIs');
  // A current synthetic live state with all write capabilities. Menus open the
  // existing workflows; they never substitute for preflight or confirmation.
  const rows=['a.txt','b.txt','c.txt'].map(name=>({id:name,path:`safe/${name}`,item:'modified',props:'normal',revision:1,kind:'file',changelist:'work'}));
  const fixture={mode:'live',workspace:'SYNTHETIC MENU TEST',root:'/synthetic/only',workspaceId:'wc',selectionVersion:1,snapshotToken:'s1',fileReadsEnabled:true,changelistWritesEnabled:true,shelfWritesEnabled:true,commitsEnabled:true,rows};
  await page.route('**/api/config',route=>route.fulfill({json:fixture}));await page.route('**/api/status',route=>route.fulfill({json:fixture}));
  await page.reload();await row(0).waitFor();await row(0).click();await row(1).locator('input').check();
  let liveBefore=requests.length;
  for(const id of ['diff','history','review','changelist','shelf','commit']){
   await row(0).click({button:'right'});await action(id).click();
   if(id==='diff'){assert.equal(await page.locator('#preview-panel.collapsed').count(),0);}
   else {assert.equal(await page.locator('#modal').isVisible(),true);if(['changelist','shelf','commit'].includes(id)){assert.equal(await page.locator(`#${id==='changelist'?'changelist':id}-confirm`).isDisabled(),true);assert.match(await page.locator('#modal-content').innerText(),/safe\/a.txt/);assert.match(await page.locator('#modal-content').innerText(),/safe\/b.txt/);assert.doesNotMatch(await page.locator('#modal-content').innerText(),/safe\/c.txt/);}await page.keyboard.press('Escape');}
   assert.equal(requests.length,liveBefore,`${id} opens only, without API dispatch`);
  }
  await row(0).click({button:'right'});await page.keyboard.press('F5');await page.locator('#read-time').filter({hasText:'已读取'}).waitFor();assert.equal(await menu.isVisible(),false);
  await row(0).click({button:'right'});await page.evaluate(()=>window.dispatchEvent(new Event('blur')));assert.equal(await menu.isVisible(),false);
  await row(0).click({button:'right'});await page.setViewportSize({width:520,height:520});assert.equal(await menu.isVisible(),false);
  await row(0).dispatchEvent('contextmenu',{button:2,clientX:519,clientY:519});const bounds=await menu.boundingBox();assert.ok(bounds.x>=0&&bounds.y>=0&&bounds.x+bounds.width<=520&&bounds.y+bounds.height<=520);
  await page.keyboard.press('Escape');
  await page.route('**/api/status',route=>route.fulfill({status:503,json:{error:'synthetic failure'}}));await row(0).click({button:'right'});await page.keyboard.press('F5');await page.locator('#live-error:not(.hidden)').waitFor();assert.equal(await menu.isVisible(),false);assert.equal(await page.locator('.file-row').count(),0);
  assert.deepEqual(errors,[]);console.log('PASS context menu browser acceptance: exact selections, no automatic API dispatch, capabilities, keyboard, invalidation, focus and viewport');
 }finally{if(browser)await browser.close();server.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
