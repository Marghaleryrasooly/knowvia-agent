/* Real browser against a separate, temporary test application only. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const base=process.env.KNOWVIA_TEST_BASE_URL;
if (!base || new URL(base).hostname!=='127.0.0.1') throw new Error('Disposable loopback application required');
const password='Synthetic-test-password!';
let stage='initialization';
(async()=>{
  const executablePath=process.env.KNOWVIA_TEST_BROWSER || [chromium.executablePath(),'C:/Program Files/Google/Chrome/Application/chrome.exe','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(p=>fs.existsSync(p));
  if(!executablePath){console.log('SKIP: no installed test browser');process.exitCode=77;return;}
  const browser=await chromium.launch({executablePath,headless:true});
  try{
    const context=await browser.newContext();const page=await context.newPage();
    await context.route('**/*',route=>new URL(route.request().url()).origin===base ? route.continue() : route.abort());
    stage='administrator sign-in';await page.goto(base+'/admin');await page.locator('#login-section').waitFor({state:'visible'});
    await page.locator('#login-form [name=work_email]').fill('browser-admin@novatech.example');
    await page.locator('#login-form [name=password]').fill(password);await page.locator('#login-form button[type=submit]').click();
    await page.locator('#dashboard').waitFor({state:'visible'});
    const view=async name=>page.locator(`#admin-navigation [data-view="${name}"]`).click();
    const settled=async()=>page.waitForFunction(()=>document.querySelector('#dashboard').getAttribute('aria-busy')==='false');
    stage='employee provisioning';await view('add');
    for(const [key,value] of Object.entries({employee_id:'browser-reader',work_email:'browser-reader@novatech.example',first_name:'Browser',last_name:'Reader',department:'IT',role:'Employee',password,password_confirmation:password}))
      await page.locator(`#provision-form [name="${key}"]`).fill(value);
    await page.locator('#provision-roles input[value=hr-reader]').check();await page.locator('#provision-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent==='Employee account created successfully.');await settled();
    await view('employees');assert.equal(await page.locator('[data-employee=browser-reader]').count(),1);
    const row=()=>page.locator('[data-employee=browser-reader]');
    stage='role update';await row().getByRole('button',{name:'Edit roles'}).click();await page.locator('#role-options input[value=it-reader]').check();
    page.once('dialog',d=>d.accept());await page.locator('#role-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent==='Employee roles updated.');await settled();
    assert.match(await row().innerText(),/it-reader/);
    stage='employee authentication and authorization';const employeeContext=await browser.newContext();
    const response=await employeeContext.request.post(base+'/api/auth/login',{data:{work_email:'browser-reader@novatech.example',password}});
    assert.equal(response.status(),200);assert.equal((await employeeContext.request.get(base+'/api/admin/accounts')).status(),403);
    stage='deactivation and session revocation';page.once('dialog',d=>d.accept());await row().getByRole('button',{name:'Deactivate',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('sessions were revoked'));await settled();
    assert.equal((await employeeContext.request.get(base+'/api/auth/me')).status(),401);
    stage='reactivation';page.once('dialog',d=>d.accept());await row().getByRole('button',{name:'Reactivate',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('reactivated'));await settled();
    assert.equal((await employeeContext.request.post(base+'/api/auth/login',{data:{work_email:'browser-reader@novatech.example',password}})).status(),200);
    await employeeContext.close();
    stage='knowledge staging';await view('knowledge');
    const upload=async content=>{
      await page.locator('#knowledge-file').setInputFiles({name:'browser-policy.md',mimeType:'text/markdown',buffer:Buffer.from(content)});
      await page.locator('#knowledge-roles input[value=hr-reader]').check();
      await page.locator('#knowledge-form button[type=submit]').click();
      await page.waitForFunction(()=>document.querySelector('#notice').textContent==='Document staged successfully. Not yet published.');await settled();
    };
    const publish=async()=>{
      page.once('dialog',d=>d.accept());await page.locator('#knowledge-publish').click();
      await page.waitForFunction(()=>document.querySelector('#notice').textContent==='Knowledge changes published.');await settled();
    };
    await upload('# Browser policy\n\n## Rule\nFollow the documented rule.');
    assert.match(await page.locator('#knowledge-status').innerText(),/Unpublished changes/);stage='knowledge publication';await publish();
    stage='knowledge replacement';page.once('dialog',d=>d.accept());await upload('# Browser policy\n\n## Rule\nFollow the updated documented rule.');await publish();
    stage='knowledge removal';page.once('dialog',d=>d.accept());await page.locator('.document-row').filter({hasText:'browser-policy.md'}).getByRole('button',{name:'Stage removal',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('removal staged'));await settled();await publish();
    assert.equal(await page.getByText('browser-policy.md',{exact:true}).count(),0);
    stage='knowledge rollback';page.once('dialog',d=>d.accept());await page.locator('#knowledge-rollback').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('version restored'));await settled();
    assert.equal(await page.getByText('browser-policy.md',{exact:true}).count(),1);
    stage='audit history';await view('activity');assert.match(await page.locator('#activity').innerText(),/Employee account created/);assert.match(await page.locator('#activity').innerText(),/Knowledge published/);
    assert.doesNotMatch(await page.locator('#activity').innerText(),/Synthetic-test-password|Follow the documented/);
    stage='same-browser employee/admin session collision';
    assert.equal((await context.request.post(base+'/api/auth/login',{data:{work_email:'browser-reader@novatech.example',password}})).status(),200);
    await page.locator('#refresh').click();await page.locator('#login-section').waitFor({state:'visible'});
    assert.match(await page.locator('#notice').innerText(),/current signed-in account/);
    assert.equal((await context.request.get(base+'/api/admin/accounts')).status(),403);
    assert.equal((await context.request.get(base+'/api/auth/me')).status(),200);
    await page.locator('#login-form [name=work_email]').fill('browser-admin@novatech.example');await page.locator('#login-form [name=password]').fill(password);
    await page.locator('#login-form button[type=submit]').click();await page.locator('#dashboard').waitFor({state:'visible'});
    await page.locator('#logout').click();await page.locator('#login-section').waitFor({state:'visible'});
    console.log('PASS disposable full-stack admin account, authentication, role, activation, knowledge and audit workflows');
  }finally{await browser.close();}
})().catch(error=>{console.error('FAIL disposable full-stack admin workflow at '+stage+' ('+error.name+')');process.exitCode=1;});
