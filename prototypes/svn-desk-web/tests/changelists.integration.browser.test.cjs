// Real bridge HTTP and label service + original backend with synthetic SVN I/O.
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
const server=spawn(process.env.PYTHON||'python3',[path.resolve(__dirname,'serve_changelist_fixture.py')]);
(async()=>{let browser;try{
 const url=await new Promise((resolve,reject)=>{let output='';server.stdout.on('data',chunk=>{output+=chunk;const m=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});server.stderr.on('data',chunk=>process.stderr.write(chunk));server.on('error',reject);server.on('exit',code=>reject(Error('Fixture exit '+code)));setTimeout(()=>reject(Error('Fixture startup timeout')),8000).unref();});
 browser=await chromium.launch({...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:960}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(url);await page.locator('#workspace-name').filter({hasText:'SYNTHETIC-changelists'}).waitFor();
 assert.equal(await page.locator('.file-row').count(),2);
 await page.locator('#select-visible').check();await page.locator('#move-changelist').click();
 assert.equal(await page.locator('#changelist-files li').count(),2);
 await page.locator('#changelist-name').fill('本地 <Feature>');await page.locator('#changelist-preflight').click();await page.locator('#changelist-confirm:not([disabled])').waitFor();
 assert.match(await page.locator('#changelist-result').innerText(),/预检通过/);
 await page.locator('#changelist-confirm').click();await page.locator('#changelist-result').filter({hasText:'修改完成'}).waitFor();await page.locator('#modal-done').click();
 assert.deepEqual(await page.locator('.group-label').allTextContents(),['本地 <Feature>']);assert.equal(await page.locator('.group-label feature').count(),0);
 await page.locator('.file-row').filter({hasText:'b @.txt'}).click();await page.locator('#move-changelist').click();await page.locator('#changelist-action').selectOption('remove');await page.locator('#changelist-preflight').click();await page.locator('#changelist-confirm:not([disabled])').waitFor();
 await page.locator('#changelist-confirm').click();await page.locator('#changelist-result').filter({hasText:'修改完成'}).waitFor();await page.locator('#modal-done').click();
 assert.equal(await page.locator('.file-row').count(),1);assert.match(await page.locator('.file-row').innerText(),/a\.txt/);
 await page.locator('.file-row').click();await page.locator('#move-changelist').click();await page.locator('#changelist-action').selectOption('remove');await page.locator('#changelist-preflight').click();await page.locator('#changelist-confirm:not([disabled])').waitFor();
 await page.locator('#changelist-confirm').click();await page.locator('#changelist-result').filter({hasText:'修改完成'}).waitFor();await page.locator('#modal-done').click();
 assert.deepEqual(await page.locator('.group-label').allTextContents(),['默认变更列表']);await page.locator('#refresh').click();await page.locator('.file-row').first().waitFor();assert.deepEqual(await page.locator('.group-label').allTextContents(),['默认变更列表']);assert.deepEqual(errors,[]);
 console.log('PASS real HTTP/catalog/service changelist browser integration with SYNTHETIC SVN transport; no real SVN claim.');
 }finally{await browser?.close();server.kill();}})().catch(error=>{console.error(error);process.exitCode=1;});
