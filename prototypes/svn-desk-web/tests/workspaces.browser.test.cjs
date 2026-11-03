// Real HTTP/catalog + synthetic status metadata. Never invokes SVN.
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const server=spawn(process.env.PYTHON||'python3',[path.join(__dirname,'serve_workspace_fixture.py')]);
(async()=>{
 let browser;
 try{
  const url=await new Promise((resolve,reject)=>{let output='';server.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(match)resolve(match[0]);});server.stderr.on('data',data=>process.stderr.write(data));server.on('error',reject);server.on('exit',code=>reject(Error('Fixture server stopped: '+code)));setTimeout(()=>reject(Error('Server timeout')),8000).unref();});
  browser=await chromium.launch({...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:960}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(url);await page.locator('#mode-pill').filter({hasText:'待选工作区'}).waitFor();
  assert.equal(await page.locator('.file-row').count(),0);
  assert.match(await page.locator('#empty-state').innerText(),/请选择一个授权工作区/);
  const open=async()=>{await page.locator('#open-workspace').click();await page.locator('.workspace-candidate').first().waitFor();};
  const choose=name=>page.locator('.workspace-candidate').filter({hasText:name}).getByRole('button',{name:'选择并读取状态'}).click();
  const loaded=async name=>{await page.locator('#workspace-name').filter({hasText:name}).waitFor();await page.locator('#refresh:not([disabled])').waitFor();};
  await open();assert.equal(await page.locator('.workspace-candidate').count(),3);
  assert.match(await page.locator('#modal-content').innerText(),/候选不代表仓库身份验证成功/);
  await page.screenshot({path:path.join(root,'evidence/web-workspace-chooser-synthetic.png')});
  await choose('SYNTHETIC-alpha');await loaded('SYNTHETIC-alpha');
  assert.match(await page.locator('#workspace-result').innerText(),/偏好已保存/);
  await page.locator('#modal-done').click();
  await page.locator('.file-row').click();assert.match(await page.locator('#selection-summary').innerText(),/已选择 1 项/);
  await page.locator('#search').fill('invisible');
  await open();await choose('SYNTHETIC-beta');await loaded('SYNTHETIC-beta');await page.locator('#modal-done').click();
  assert.equal(await page.locator('#search').inputValue(),'');
  assert.match(await page.locator('#selection-summary').innerText(),/已选择 0 项/);
  assert.equal(await page.locator('.file-row').count(),1);
  await page.screenshot({path:path.join(root,'evidence/web-workspace-selected-synthetic.png')});
  // Closing during a submitted read does not reopen the dialog or retain stale identity.
  await open();await choose('SYNTHETIC-alpha');await page.locator('#modal-done').click();await loaded('SYNTHETIC-alpha');
  assert.equal(await page.locator('#modal').isVisible(),false);
  assert.match(await page.locator('#selection-summary').innerText(),/已选择 0 项/);
  // Failed switch returns no stale/demo rows; explicit refresh recovers previous current WC.
  await open();await choose('SYNTHETIC-broken');await page.locator('#live-error:not(.hidden)').waitFor();
  assert.equal(await page.locator('.file-row').count(),0);
  assert.match(await page.locator('#workspace-result').innerText(),/没有切换/);
  assert.doesNotMatch(await page.locator('#modal-content').innerText(),/raw secret/);
  await page.locator('#modal-done').click();await page.locator('#refresh').click();await loaded('SYNTHETIC-alpha');
  assert.equal(await page.locator('.file-row').count(),1);
  // In-flight discovery must not overwrite a closed dialog or a newer Help view.
  let release;const gate=new Promise(resolve=>release=resolve);
  await page.route('**/api/workspaces',async route=>{await gate;await route.continue();},{times:1});
  await page.locator('#open-workspace').click();await page.locator('#modal-done').click();await page.locator('#help').click();
  release();await page.waitForResponse(response=>response.url().endsWith('/api/workspaces'));
  assert.match(await page.locator('#modal-title').innerText(),/Web 原型/);
  assert.equal(await page.locator('.workspace-candidate').count(),0);
  await page.keyboard.press('Escape');
  // Repeated click does not duplicate selection; modal result reflects current choice.
  let selections=0;page.on('request',request=>{if(request.url().endsWith('/api/workspace-selection'))selections++;});
  await open();const button=page.locator('.workspace-candidate').filter({hasText:'SYNTHETIC-beta'}).getByRole('button');
  await button.evaluate(element=>{element.click();element.click();});await loaded('SYNTHETIC-beta');
  assert.equal(selections,1);await page.locator('#modal-done').click();
  await page.reload();await loaded('SYNTHETIC-beta');assert.equal(await page.locator('.file-row').count(),1);
  assert.deepEqual(errors,[]);
  console.log('PASS actual Chromium: synthetic catalog discovery, switching, scope reset, save feedback, close/reopen, stale response, duplicate click, failure recovery. NOT real SVN.');
 }finally{if(browser)await browser.close();server.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
