/* Offline browser behavior: every request is intercepted; no live service. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../..');
const assets = path.join(root, 'src/knowvia/static');
const roles = [{name:'employee',permissions:[]},{name:'hr-reader',permissions:['knowledge.hr.read']},
  {name:'it-reader',permissions:['knowledge.it.read']},{name:'administrator',permissions:['admin.accounts.manage']}];
const account = {employee_id:'reader',first_name:'Test',last_name:'Reader',work_email:'reader@novatech.example',department:'IT',role:'Employee',active:true,account_status:'ACTIVE',roles:['employee']};
async function fixture(browser, {width=1280, employeeReference=false, initialStatus=200, postStatus=200, postCode=null, refreshFails=false, holdPost=false, abortPost=false, malformedPost=false, rows=[account], knowledge={active_version:'reference',revision:'clean',staged:false,rollback_available:false,documents:[]}, events=[]}={}) {
  const context = await browser.newContext({viewport:{width,height:900}});
  const page = await context.newPage();
  const state = {posts:[],saved:false,failReads:false,sessionExpired:false,adminStatus:initialStatus};
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.hostname !== 'knowvia.test') {
      if(employeeReference)return route.abort(); // No remote font or provider requests.
      throw new Error('Unexpected network destination');
    }
    const pathname = url.pathname;
    const json = (value,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
    if (pathname === '/admin') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(assets,'admin.html'),'utf8')});
    if (pathname === '/') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(assets,'index.html'),'utf8')});
    if (pathname.startsWith('/static/')) return route.fulfill({contentType:pathname.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(path.join(assets,path.basename(pathname)),'utf8')});
    if (request.method() === 'POST') {
      state.posts.push({path:pathname,body:request.postDataJSON()});
      if (holdPost) await new Promise(resolve=>{state.releasePost=resolve;});
      if (abortPost) return route.abort('failed');
      if (malformedPost) return route.fulfill({status:200,contentType:'application/json',body:'{'});
      if (postStatus !== 200) return json({error:'must not reflect arbitrary server text',code:postCode},postStatus);
      state.saved=true; if (refreshFails) state.failReads=true;
      return json({updated:true});
    }
    if (state.sessionExpired) return json({error:'Authentication required.'},401);
    if(pathname==='/api/config')return json({registration_enabled:false,development_identity_override:false,developer_details_enabled:false});
    if(pathname==='/api/auth/me')return json({error:'Authentication required.'},401);
    if (state.failReads) return json({error:'Unavailable'},503);
    if (pathname === '/api/admin/me') return json({administrator:true,employee_id:'admin'},state.adminStatus);
    if (pathname === '/api/admin/roles') return json({roles});
    if (pathname === '/api/admin/accounts') return json({accounts:rows});
    if (pathname === '/api/admin/audit') return json({events});
    if (pathname === '/api/admin/knowledge') return json(knowledge);
    throw new Error('Unexpected offline request '+pathname);
  });
  await page.goto('http://knowvia.test/'+(employeeReference ? '' : 'admin'));
  await page.locator(employeeReference ? '#auth-content' : initialStatus===200 ? '#dashboard' : '#login-section').waitFor({state:'visible'});
  return {page,state,context};
}
async function view(page,name) {
  const nav=page.locator(`[data-view="${name}"]`);
  if (!(await nav.first().isVisible())) await page.locator('#menu-toggle').click();
  if (await nav.count()) await nav.first().click();
}
async function employeeForm(page, password='Synthetic-test-password!') {
  await view(page,'add');
  const form=page.locator('#provision-form');
  for (const [key,value] of Object.entries({employee_id:'new-reader',work_email:'new-reader@novatech.example',first_name:'New',last_name:'Reader',department:'IT',role:'Employee',password,password_confirmation:password}))
    await form.locator(`[name="${key}"]`).fill(value);
}
const tests = [
  ['invitation onboarding sends trusted roles without password fields',async browser=>{
    const {page,state,context}=await fixture(browser);
    await employeeForm(page);
    await page.locator('#onboarding-method').selectOption('invite');
    assert.equal(await page.locator('#provision-form [name=password]').isDisabled(),true);
    await page.locator('#provision-roles input[value=it-reader]').check();
    await page.locator('#provision-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('invitation email sent'));
    assert.equal(state.posts.length,1);
    assert.equal(state.posts[0].path,'/api/admin/accounts/invite');
    assert.deepEqual(state.posts[0].body.roles,['employee','it-reader']);
    for(const key of ['password','password_confirmation','onboarding'])assert.equal(key in state.posts[0].body,false);
    await context.close();
  }],
  ['admin sign-in follows employee card and control conventions without registration',async browser=>{
    for(const width of [360,1280]){
      const reference=await fixture(browser,{width,employeeReference:true});const review=await fixture(browser,{width,initialStatus:401});
      const geometry=async(page,card,input)=>page.locator(card).evaluate((e,selector)=>({width:e.getBoundingClientRect().width,radius:getComputedStyle(e).borderRadius,inputHeight:document.querySelector(selector).getBoundingClientRect().height}),input);
      const employee=await geometry(reference.page,'.auth-card','#signin-email');const admin=await geometry(review.page,'.login','#login-form [name=work_email]');
      assert.equal(admin.width,employee.width);assert.equal(admin.radius,employee.radius);assert.equal(admin.inputHeight,employee.inputHeight);
      assert.equal(await review.page.getByRole('tab',{name:'Create account'}).count(),0);assert.equal(await review.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      if(process.env.KNOWVIA_ADMIN_SCREENSHOT_DIR){fs.mkdirSync(process.env.KNOWVIA_ADMIN_SCREENSHOT_DIR,{recursive:true});for(const [name,value] of [['employee-login',reference],['admin-login',review]])await value.page.screenshot({path:path.join(process.env.KNOWVIA_ADMIN_SCREENSHOT_DIR,`${name}-${width}.png`),fullPage:true});}
      await reference.context.close();await review.context.close();
    }
  }],
  ['admin password visibility is accessible, keyboard operable and never submits',async browser=>{
    const {page,state,context}=await fixture(browser,{initialStatus:401,postStatus:401});const field=page.locator('#admin-password');const toggle=page.locator('#admin-password-toggle');
    await field.focus();assert.notEqual(await field.evaluate(e=>getComputedStyle(e).boxShadow),'none');
    const session=await context.newCDPSession(page);await session.send('DOM.enable');await session.send('CSS.enable');
    const {root}=await session.send('DOM.getDocument');const {nodeId}=await session.send('DOM.querySelector',{nodeId:root.nodeId,selector:'#admin-password'});
    await session.send('CSS.forcePseudoState',{nodeId,forcedPseudoClasses:['autofill']});
    assert.equal(await field.evaluate(e=>getComputedStyle(e).webkitTextFillColor),'rgb(38, 52, 61)');
    await session.send('CSS.forcePseudoState',{nodeId,forcedPseudoClasses:[]});
    await field.fill('Synthetic-test-password!');await toggle.focus();await page.keyboard.press('Enter');
    assert.equal(await field.getAttribute('type'),'text');assert.equal(await toggle.getAttribute('aria-label'),'Hide password');assert.equal(await toggle.getAttribute('aria-pressed'),'true');
    assert.equal(await field.inputValue(),'Synthetic-test-password!');assert.equal(state.posts.length,0);await page.keyboard.press('Space');assert.equal(await field.getAttribute('type'),'password');
    await toggle.click();await page.locator('#login-form [name=work_email]').fill('admin@novatech.example');await page.locator('#login-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('credentials'));assert.equal(await field.inputValue(),'');assert.equal(await field.getAttribute('type'),'password');assert.equal(await toggle.getAttribute('aria-pressed'),'false');await context.close();
  }],
  ['compact notifications occupy their own space and never overlap page controls',async browser=>{
    for(const width of [360,768,1280])for(const postStatus of [200,400]){
      const {page,context}=await fixture(browser,{width,postStatus,postCode:'IDENTITY_CONFLICT'});await employeeForm(page);await page.locator('#provision-form button[type=submit]').click();
      await page.waitForFunction(()=>!document.querySelector('#notification').hidden);
      const overlap=await page.locator('#notification').evaluate(toast=>{const a=toast.getBoundingClientRect();return [...document.querySelectorAll('button,a,input,select')].some(e=>{const b=e.getBoundingClientRect();return !toast.contains(e)&&b.width&&b.height&&a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;});});
      assert.equal(overlap,false);assert.ok((await page.locator('#notification').boundingBox()).width<=340);await context.close();
    }
  }],
  ['fresh unauthenticated visit is not a session-expiration error',async browser=>{
    const {page,context}=await fixture(browser,{initialStatus:401});
    assert.doesNotMatch(await page.locator('#notice').textContent(),/expired|unavailable|failed/i);await context.close();
  }],
  ['comparable provisioning inputs have matching computed heights',async browser=>{
    const {page,context}=await fixture(browser);await employeeForm(page);
    const heights=await page.locator('#provision-form input:not([type=checkbox])').evaluateAll(inputs=>inputs.map(e=>e.getBoundingClientRect().height));
    assert.ok(Math.max(...heights)-Math.min(...heights)<1,'Input heights differ: '+heights.join(','));await context.close();
  }],
  ['employee session on initial admin visit requires admin sign-in, not expiration',async browser=>{
    const {page,state,context}=await fixture(browser,{initialStatus:403});
    assert.match(await page.locator('#notice').innerText(),/administrator access/);assert.doesNotMatch(await page.locator('#notice').innerText(),/expired/);
    assert.equal(state.posts.length,0);await context.close();
  }],
  ['session replaced by employee is locked without logging that employee out',async browser=>{
    const {page,state,context}=await fixture(browser);state.adminStatus=403;await page.locator('#refresh').click();
    await page.locator('#login-section').waitFor({state:'visible'});assert.match(await page.locator('#notice').innerText(),/current signed-in account/);
    assert.equal(state.posts.length,0);assert.equal(await page.locator('#accounts').innerText(),'');await context.close();
  }],
  ['invalid credentials are distinct from initial unauthenticated check',async browser=>{
    const {page,state,context}=await fixture(browser,{initialStatus:401,postStatus:401});
    await page.locator('#login-form [name=work_email]').fill('admin@novatech.example');await page.locator('#login-form [name=password]').fill('Synthetic-test-password!');
    await page.locator('#login-form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('credentials'));
    assert.equal(await page.locator('#login-form [name=password]').inputValue(),'');assert.equal(state.posts.length,1);await context.close();
  }],
  ['success toast dismisses after 4.5 seconds without moving focus',async browser=>{
    const {page,context}=await fixture(browser);await page.clock.install();await employeeForm(page);
    await page.locator('#provision-form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('created successfully'));
    assert.equal(await page.locator('#notification').isVisible(),true);assert.equal(await page.locator('#notice').getAttribute('role'),'status');
    assert.equal(await page.locator('#notice-dismiss').evaluate(e=>e===document.activeElement),false);
    await page.clock.fastForward(4400);assert.equal(await page.locator('#notification').isVisible(),true);
    await page.clock.fastForward(200);assert.equal(await page.locator('#notification').isVisible(),false);await context.close();
  }],
  ['success clears on navigation and is not repeated by rendering',async browser=>{
    const {page,state,context}=await fixture(browser);await employeeForm(page);await page.locator('#provision-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('created successfully'));await view(page,'employees');
    assert.equal(await page.locator('#notification').isVisible(),false);await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent==='Dashboard refreshed.');assert.equal(state.posts.length,1);await context.close();
  }],
  ['saved-change refresh warning persists until explicitly dismissed',async browser=>{
    const {page,context}=await fixture(browser,{refreshFails:true});await page.clock.install();await employeeForm(page);await page.locator('#provision-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#notice').className==='warning');await page.clock.fastForward(60000);
    assert.equal(await page.locator('#notification').isVisible(),true);assert.match(await page.locator('#notice').innerText(),/created successfully/);
    await view(page,'employees');assert.equal(await page.locator('#notification').isVisible(),true);await page.locator('#notice-dismiss').click();
    assert.equal(await page.locator('#notification').isVisible(),false);await context.close();
  }],
  ['critical errors persist, use alert semantics and can be dismissed',async browser=>{
    const {page,context}=await fixture(browser,{postStatus:400,postCode:'IDENTITY_CONFLICT'});await page.clock.install();await employeeForm(page);
    await page.locator('#provision-form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('already exists'));
    assert.equal(await page.locator('#notice').getAttribute('role'),'alert');await page.clock.fastForward(60000);assert.equal(await page.locator('#notification').isVisible(),true);
    await page.locator('#notice-dismiss').click();assert.equal(await page.locator('#notification').isVisible(),false);await context.close();
  }],
  ['knowledge disabled controls have visible explanations',async browser=>{
    const {page,context}=await fixture(browser);await view(page,'knowledge');assert.equal(await page.locator('#knowledge-publish').isDisabled(),true);
    assert.match(await page.locator('#publication-help').innerText(),/no changes are staged/);assert.match(await page.locator('#rollback-help').innerText(),/previous published version/);await context.close();
  }],
  ['all five views fit narrow widths and controls retain focus states',async browser=>{
    for(const width of [360,768,1280]){
      const {page,context}=await fixture(browser,{width});
      for(const name of ['overview','employees','add','knowledge','activity']){
        await view(page,name);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,name+' overflow at '+width);
        if(process.env.KNOWVIA_ADMIN_SCREENSHOT_DIR){fs.mkdirSync(process.env.KNOWVIA_ADMIN_SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.KNOWVIA_ADMIN_SCREENSHOT_DIR,`${name}-${width}.png`),fullPage:true});}
      }
      await view(page,'add');const input=page.locator('[name=employee_id]');await input.focus();
      assert.equal(await input.evaluate(e=>getComputedStyle(e).height),'44px');assert.notEqual(await input.evaluate(e=>getComputedStyle(e).outlineStyle),'none');
      assert.equal(await page.locator('#provision-form [name=password]').getAttribute('autocomplete'),'new-password');
      await context.close();
    }
  }],
  ['Chromium autofill styling preserves text contrast and password-manager attributes',async browser=>{
    const {page,context}=await fixture(browser);await employeeForm(page);
    const session=await context.newCDPSession(page);await session.send('DOM.enable');await session.send('CSS.enable');
    const {root}=await session.send('DOM.getDocument');const {nodeId}=await session.send('DOM.querySelector',{nodeId:root.nodeId,selector:'#provision-form [name=work_email]'});
    await session.send('CSS.forcePseudoState',{nodeId,forcedPseudoClasses:['autofill']});
    const style=await page.locator('#provision-form [name=work_email]').evaluate(e=>({active:e.matches(':autofill'),fill:getComputedStyle(e).webkitTextFillColor,shadow:getComputedStyle(e).boxShadow}));
    if(style.active){assert.equal(style.fill,'rgb(38, 52, 61)');assert.match(style.shadow,/inset/);}else console.log('NOTE native autofill simulation unavailable; real password-manager review still required');
    await context.close();
  }],
  ['saved mutation stays successful when refresh fails',async browser => {
    const {page,state,context}=await fixture(browser,{refreshFails:true});
    await employeeForm(page); await page.locator('#provision-form button[type="submit"]').click();
    await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('refresh'));
    assert.match(await page.locator('#notice').innerText(),/created|saved/i);
    assert.equal(state.posts.length,1); assert.equal(state.saved,true); await context.close();
  }],
  ['origin denial does not sign administrator out',async browser => {
    const {page,state,context}=await fixture(browser,{postStatus:403,postCode:'ORIGIN_REJECTED'});
    await employeeForm(page); await page.locator('#provision-form button[type="submit"]').click();
    await page.waitForFunction(()=>document.getElementById('notice').textContent.includes('origin'));
    assert.equal(await page.locator('#dashboard').isVisible(),true); assert.equal(state.posts.length,1); await context.close();
  }],
  ['permission denial preserves session and employee form',async browser=>{
    const {page,state,context}=await fixture(browser,{postStatus:403});await employeeForm(page);
    await page.locator('#provision-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('permission'));
    assert.equal(await page.locator('#dashboard').isVisible(),true);
    assert.equal(await page.locator('[name=employee_id]').inputValue(),'new-reader');assert.equal(state.posts.length,1);await context.close();
  }],
  ['expired session clears private view and drafts',async browser=>{
    const {page,state,context}=await fixture(browser);await employeeForm(page);state.sessionExpired=true;
    await page.locator('#refresh').click();await page.locator('#login-section').waitFor({state:'visible'});
    assert.equal(await page.locator('[name=employee_id]').inputValue(),'');assert.equal(await page.locator('#accounts').innerText(),'');
    assert.equal(state.posts.length,0);await context.close();
  }],
  ['duplicate identity feedback is safe and preserves non-secret fields',async browser=>{
    const {page,state,context}=await fixture(browser,{postStatus:400,postCode:'IDENTITY_CONFLICT'});await employeeForm(page);
    await page.locator('#provision-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#employee-form-error').textContent.includes('already exists'));
    assert.equal(await page.locator('[name=employee_id]').inputValue(),'new-reader');
    assert.equal(await page.locator('#provision-form [name=password]').inputValue(),'');
    assert.doesNotMatch(await page.locator('#notice').innerText(),/arbitrary server text/);assert.equal(state.posts.length,1);await context.close();
  }],
  ['required fields, password bounds, mismatch and missing roles block submission',async browser=>{
    const {page,state,context}=await fixture(browser);await view(page,'add');
    await page.locator('#provision-form button[type=submit]').click();assert.equal(state.posts.length,0);
    await employeeForm(page,'short');assert.equal(await page.locator('#provision-form').evaluate(f=>f.checkValidity()),false);
    assert.equal(await page.locator('#provision-form [name=password]').getAttribute('maxlength'),'128');
    await employeeForm(page);await page.locator('#provision-form [name=password_confirmation]').fill('Different-test-password!');
    await page.locator('#provision-form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#employee-form-error').textContent.includes('match'));
    await employeeForm(page);await page.locator('#provision-roles input:checked').uncheck();
    await page.locator('#provision-form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#employee-form-error').textContent.includes('trusted role'));
    assert.equal(state.posts.length,0);await context.close();
  }],
  ['pending submission cannot be duplicated or edited',async browser=>{
    const {page,state,context}=await fixture(browser,{holdPost:true});await employeeForm(page);
    await page.locator('#provision-form').evaluate(f=>f.requestSubmit());
    await page.waitForFunction(()=>document.querySelector('#dashboard').getAttribute('aria-busy')==='true');
    await page.locator('#provision-form').evaluate(f=>f.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true})));
    assert.equal(await page.locator('#provision-form').evaluate(f=>f.inert),true);
    assert.equal(await page.locator('#provision-form button[type=submit]').isDisabled(),true);
    await page.waitForTimeout(30);assert.equal(state.posts.length,1);state.releasePost();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('created'));await context.close();
  }],
  ['employee drafts and selected roles survive navigation and refresh',async browser=>{
    const {page,state,context}=await fixture(browser);await employeeForm(page);await page.locator('#provision-roles input[value=hr-reader]').check();
    await view(page,'overview');await page.locator('#refresh').click();await view(page,'add');
    assert.equal(await page.locator('[name=employee_id]').inputValue(),'new-reader');assert.equal(await page.locator('#provision-roles input[value=hr-reader]').isChecked(),true);
    assert.equal(state.posts.length,0);await context.close();
  }],
  ['explicit role editor, discard confirmation and trusted save payload',async browser=>{
    const {page,state,context}=await fixture(browser);await view(page,'employees');assert.equal(await page.locator('#accounts input').count(),0);
    await page.getByRole('button',{name:'Edit roles',exact:true}).click();await page.locator('#role-options input[value=hr-reader]').check();
    page.once('dialog',dialog=>dialog.dismiss());await page.locator('#role-cancel').click();assert.equal(await page.locator('#role-dialog').evaluate(d=>d.open),true);
    page.once('dialog',dialog=>dialog.accept());await page.locator('#role-cancel').click();assert.equal(await page.locator('#role-dialog').evaluate(d=>d.open),false);assert.equal(state.posts.length,0);
    await page.getByRole('button',{name:'Edit roles',exact:true}).click();assert.equal(await page.locator('#role-options input[value=hr-reader]').isChecked(),false);
    await page.locator('#role-options input[value=it-reader]').check();page.once('dialog',dialog=>dialog.accept());await page.locator('#role-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('roles updated'));
    assert.deepEqual(state.posts,[{path:'/api/admin/accounts/reader/roles',body:{roles:['employee','it-reader']}}]);await context.close();
  }],
  ['status labels are distinct and active badge is green',async browser=>{
    const rows=[account,{...account,employee_id:'off',active:false,account_status:'DEACTIVATED'},
      {...account,employee_id:'directory',account_status:'NOT_REGISTERED'}, {...account,employee_id:'inactive-directory',account_status:'NOT_REGISTERED',active:false}];
    const {page,context}=await fixture(browser,{rows});await view(page,'employees');
    for(const text of ['Active','Deactivated','No sign-in account','Inactive directory record']) assert.equal(await page.locator('#accounts').getByText(text,{exact:true}).count(),1);
    const color=await page.locator('.badge.active').evaluate(e=>getComputedStyle(e).color);assert.equal(color,'rgb(36, 96, 68)');
    assert.equal(await page.locator('#accounts button').filter({hasText:'Delete'}).count(),0);await context.close();
  }],
  ['activation has confirmation and operation-specific feedback',async browser=>{
    const {page,state,context}=await fixture(browser);await view(page,'employees');page.once('dialog',d=>d.dismiss());
    await page.getByRole('button',{name:'Deactivate',exact:true}).click();assert.equal(state.posts.length,0);
    page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'Deactivate',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('sessions were revoked'));
    assert.deepEqual(state.posts[0],{path:'/api/admin/accounts/reader/activation',body:{active:false}});await context.close();
  }],
  ['knowledge drafts survive refresh; staging never automatically publishes',async browser=>{
    const {page,state,context}=await fixture(browser);await view(page,'knowledge');
    await page.locator('#knowledge-file').setInputFiles({name:'sample.md',mimeType:'text/markdown',buffer:Buffer.from('# Sample\n\n## Policy\nDocumented rule.')});
    await page.locator('#knowledge-roles input[value=hr-reader]').check();await view(page,'overview');await page.locator('#refresh').click();await view(page,'knowledge');
    assert.equal(await page.locator('#knowledge-file').evaluate(e=>e.files[0].name),'sample.md');assert.equal(await page.locator('#knowledge-roles input[value=hr-reader]').isChecked(),true);
    await page.locator('#knowledge-form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Not yet published'));
    assert.equal(state.posts.length,1);assert.equal(state.posts[0].path,'/api/admin/knowledge/stage');assert.deepEqual(state.posts[0].body.roles,['hr-reader']);await context.close();
  }],
  ['publication and rollback require explicit confirmation',async browser=>{
    const knowledge={active_version:'version-1',revision:'draft-2',staged:true,rollback_available:true,documents:[]};
    const {page,state,context}=await fixture(browser,{knowledge});await view(page,'knowledge');
    assert.equal(await page.locator('#knowledge-rollback').isDisabled(),true);page.once('dialog',d=>d.dismiss());await page.locator('#knowledge-publish').click();assert.equal(state.posts.length,0);
    page.once('dialog',d=>d.accept());await page.locator('#knowledge-publish').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent==='Knowledge changes published.');
    assert.deepEqual(state.posts[0],{path:'/api/admin/knowledge/publish',body:{revision:'draft-2'}});await context.close();
    const next=await fixture(browser,{knowledge:{...knowledge,staged:false,revision:'clean'}});await view(next.page,'knowledge');next.page.once('dialog',d=>d.accept());await next.page.locator('#knowledge-rollback').click();
    await next.page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('restored'));assert.equal(next.state.posts[0].path,'/api/admin/knowledge/rollback');await next.context.close();
  }],
  ['unconfirmed transport and unreadable responses never retry mutations',async browser=>{
    for(const options of [{abortPost:true},{malformedPost:true}]){
      const {page,state,context}=await fixture(browser,options);await employeeForm(page);await page.locator('#provision-form button[type=submit]').click();
      await page.waitForFunction(()=>document.querySelector('#notice').className==='error');assert.match(await page.locator('#notice').innerText(),/check the current state/i);
      assert.equal(state.posts.length,1);assert.equal(await page.locator('[name=employee_id]').inputValue(),'new-reader');await context.close();
    }
  }],
  ['audit and identity values render as text, not executable markup',async browser=>{
    const attack='<img src=x onerror="window.injected=true">';
    const {page,context}=await fixture(browser,{rows:[{...account,first_name:attack}],events:[{operation:'PROVISION',actor:attack,target:'reader',timestamp:'2026-10-17T10:00:00Z',roles:'["employee"]'}]});
    await view(page,'employees');assert.equal(await page.locator('#accounts img').count(),0);assert.match(await page.locator('#accounts').innerText(),/onerror/);
    await view(page,'activity');assert.equal(await page.locator('#activity img').count(),0);assert.equal(await page.locator('#activity details').count(),1);assert.equal(await page.evaluate(()=>window.injected),undefined);await context.close();
  }],
  ['mobile navigation, keyboard focus and layout remain usable',async browser=>{
    for(const width of [360,768,1280]){
      const {page,context}=await fixture(browser,{width});await view(page,'employees');
      assert.equal(await page.locator('#view-title').evaluate(e=>e===document.activeElement),true);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      if (process.env.KNOWVIA_ADMIN_SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.KNOWVIA_ADMIN_SCREENSHOT_DIR,{recursive:true});
        await page.screenshot({path:path.join(process.env.KNOWVIA_ADMIN_SCREENSHOT_DIR,`employees-${width}.png`),fullPage:true});
      }
      await page.getByRole('button',{name:'Edit roles',exact:true}).focus();await page.keyboard.press('Enter');assert.equal(await page.locator('#role-dialog').evaluate(d=>d.open),true);
      await page.keyboard.press('Escape');assert.equal(await page.locator('#role-dialog').evaluate(d=>d.open),false);
      const outline=await page.getByRole('button',{name:'Edit roles',exact:true}).evaluate(e=>getComputedStyle(e).outlineStyle);assert.notEqual(outline,'none');
      await context.close();
    }
  }],
  ['sign-in and sign-out use existing session endpoints and clear passwords',async browser=>{
    const {page,state,context}=await fixture(browser);state.sessionExpired=true;await page.locator('#refresh').click();
    await page.locator('#login-section').waitFor({state:'visible'});state.sessionExpired=false;
    await page.locator('#login-form [name=work_email]').fill('admin@novatech.example');await page.locator('#login-form [name=password]').fill('Synthetic-test-password!');
    await page.locator('#login-form button[type=submit]').click();await page.locator('#dashboard').waitFor({state:'visible'});
    assert.equal(await page.locator('#login-form [name=password]').inputValue(),'');
    await page.locator('#logout').click();await page.locator('#login-section').waitFor({state:'visible'});
    assert.deepEqual(state.posts.map(p=>p.path),['/api/auth/login','/api/auth/logout']);await context.close();
  }],
  ['knowledge replacement and removal remain explicitly staged',async browser=>{
    const knowledge={active_version:'version-1',revision:'clean',staged:false,rollback_available:true,documents:[{name:'sample.md',skill:'hr',bytes:80,roles:['hr-reader']}]};
    const {page,state,context}=await fixture(browser,{knowledge});await view(page,'knowledge');
    await page.locator('#knowledge-file').setInputFiles({name:'sample.md',mimeType:'text/markdown',buffer:Buffer.from('# Updated\n\n## Policy\nUpdated rule.')});
    await page.locator('#knowledge-roles input[value=hr-reader]').check();page.once('dialog',d=>d.dismiss());await page.locator('#knowledge-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#dashboard').getAttribute('aria-busy')==='false');assert.equal(state.posts.length,0);
    page.once('dialog',d=>d.accept());await page.locator('#knowledge-form button[type=submit]').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Document staged'));
    page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'Stage removal',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('removal staged'));
    assert.deepEqual(state.posts.map(p=>p.path),['/api/admin/knowledge/stage','/api/admin/knowledge/remove']);await context.close();
  }],
  ['knowledge rejection retains draft and names the failed operation',async browser=>{
    const {page,state,context}=await fixture(browser,{postStatus:400});await view(page,'knowledge');
    await page.locator('#knowledge-file').setInputFiles({name:'sample.md',mimeType:'text/markdown',buffer:Buffer.from('# Sample\n\n## Policy\nRule.')});
    await page.locator('#knowledge-roles input[value=hr-reader]').check();await page.locator('#knowledge-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Document staging could not'));
    assert.equal(await page.locator('#knowledge-file').evaluate(e=>e.files.length),1);assert.equal(state.posts.length,1);await context.close();
  }],
];
(async()=>{
  const executablePath = process.env.KNOWVIA_TEST_BROWSER || [chromium.executablePath(),
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(p=>fs.existsSync(p));
  if (!executablePath) { console.log('SKIP: no installed test browser'); process.exitCode=77;return; }
  const browser=await chromium.launch({executablePath,headless:true}); let failed=0;
  try { for (const [name,test] of tests) {
    try {await test(browser);console.log('PASS '+name);} catch(error){failed++;console.log('FAIL '+name+' '+error.message);}
  }} finally {await browser.close();}
  process.exitCode=failed?1:0;
})();
