/* Every request is intercepted. Tokens/passwords below are synthetic fixtures. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const assets=path.resolve(__dirname,'../../src/knowvia/static');
async function fixture(browser,url,width=1100,status={email_verified:false,email_delivery_enabled:true},failure=false,options={}){
  const context=await browser.newContext({viewport:{width,height:900}});
  const page=await context.newPage(), posts=[],state={hold:!!options.hold};
  await page.route('**/*',async route=>{
    const request=route.request(),parsed=new URL(request.url());
    assert.equal(parsed.hostname,'knowvia.test');
    if(['/password','/account'].includes(parsed.pathname))return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(assets,'password.html'),'utf8')});
    if(parsed.pathname==='/')return route.fulfill({contentType:'text/html',body:'<!doctype html><h1>Sign in</h1>'});
    if(parsed.pathname.startsWith('/static/'))return route.fulfill({contentType:parsed.pathname.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(path.join(assets,path.basename(parsed.pathname)),'utf8')});
    if(request.method()==='POST'){
      posts.push({path:parsed.pathname,body:request.postDataJSON()});
      if(state.hold)await new Promise(resolve=>{state.release=resolve;});
      if(options.abort)return route.abort('failed');
      if(options.rateLimit)return route.fulfill({status:429,contentType:'application/json',body:JSON.stringify({error:'Please wait before trying again.'})});
      if(failure)return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'The request could not be completed.'})});
      return route.fulfill({contentType:'application/json',body:JSON.stringify({completed:true,changed:true,sent:true,message:'Generic reset response.'})});
    }
    return route.fulfill({contentType:'application/json',body:JSON.stringify(status)});
  });
  await page.goto(url);
  return {context,page,posts,state};
}
(async()=>{
  let browser;
  try{browser=await chromium.launch({channel:'msedge',headless:true});}
  catch(_){try{browser=await chromium.launch({channel:'chrome',headless:true});}catch(_){process.exit(77);}}
  try{
    const token='A'.repeat(43);
    let f=await fixture(browser,'https://knowvia.test/password#purpose=invite&token='+token);
    await f.page.locator('#redeem-form').waitFor({state:'visible'});
    assert.equal(new URL(f.page.url()).hash,'');
    await f.page.locator('#redeem-form [name=password]').fill('Synthetic-new-phrase!');
    await f.page.locator('#redeem-form [name=password_confirmation]').fill('Different-synthetic-phrase!');
    await f.page.locator('#redeem-submit').click();
    assert.equal(f.posts.length,0);
    await f.page.locator('#redeem-form [name=password_confirmation]').fill('Synthetic-new-phrase!');
    await f.page.locator('#redeem-submit').click();
    await f.page.locator('#redeem-form').waitFor({state:'hidden'});
    assert.equal(f.posts.length,1);assert.equal(f.posts[0].body.token,token);
    assert.equal(await f.page.evaluate(()=>localStorage.length+sessionStorage.length),0);
    assert(!(await f.page.locator('body').innerText()).includes(token));
    assert.equal(await f.page.locator('#success-title').innerText(),'Your account is ready. You can now sign in.');
    assert.equal(await f.page.locator('#success-title').evaluate(e=>e===document.activeElement),true);
    assert.equal(await f.page.locator('#success-section a').getAttribute('href'),'/');
    await f.page.locator('#redeem-form').evaluate(e=>e.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
    assert.equal(f.posts.length,1,'Completed invitation cannot submit twice');
    await f.page.locator('#success-section a').click();assert.equal(new URL(f.page.url()).pathname,'/');
    assert(await f.page.getByRole('heading',{name:'Sign in',exact:true}).isVisible());assert.equal(f.posts.length,1);
    await f.context.close();
    for(const width of [360,1280]){
      f=await fixture(browser,'https://knowvia.test/password',width,undefined,false,{hold:true});
      assert.equal(await f.page.getByLabel('Account email',{exact:true}).count(),1);
      assert.match(await f.page.locator('#description').innerText(),/account email/);
      await f.page.getByLabel('Account email',{exact:true}).fill('employee@example.org');
      await f.page.locator('#forgot-form button').click();
      await f.page.waitForFunction(()=>document.querySelector('#forgot-form').getAttribute('aria-busy')==='true');
      assert.equal(await f.page.locator('#forgot-form button').isDisabled(),true);
      assert.match(await f.page.locator('#forgot-form button').innerText(),/Requesting/);
      await f.page.locator('#forgot-form').evaluate(e=>e.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
      assert.equal(f.posts.length,1);f.state.release();
      await f.page.waitForFunction(()=>document.querySelector('#forgot-form button').textContent==='Request another link');
      assert.match(await f.page.locator('#forgot-followup').innerText(),/delivery is not guaranteed/);
      assert.equal(await f.page.locator('#forgot-form button').isEnabled(),true);
      assert(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      if(process.env.KNOWVIA_ACCOUNT_UX_SCREENSHOT_DIR){fs.mkdirSync(process.env.KNOWVIA_ACCOUNT_UX_SCREENSHOT_DIR,{recursive:true});await f.page.screenshot({path:path.join(process.env.KNOWVIA_ACCOUNT_UX_SCREENSHOT_DIR,`forgot-${width}.png`),fullPage:true});}
      f.state.hold=false;await f.page.locator('#forgot-form button').click();
      await f.page.waitForFunction(()=>document.querySelector('#forgot-form').getAttribute('aria-busy')!== 'true');
      assert.equal(f.posts.length,2,'Resend requires a new explicit user request');assert.deepEqual(f.posts[0],f.posts[1]);
      await f.context.close();
    }
    for(const options of [{abort:true},{rateLimit:true}]){
      f=await fixture(browser,'https://knowvia.test/password',390,undefined,false,options);
      await f.page.getByLabel('Account email',{exact:true}).fill('employee@example.org');await f.page.locator('#forgot-form button').click();
      await f.page.waitForFunction(()=>document.querySelector('#feedback').className==='error');
      assert.match(await f.page.locator('#feedback').innerText(),/No automatic retry/);
      assert.equal(f.posts.length,1);assert(await f.page.locator('#forgot-form button').isEnabled());await f.context.close();
    }
    for(const purpose of ['invite','reset']){
      f=await fixture(browser,'https://knowvia.test/password#purpose='+purpose+'&token='+token,390);
      assert.match(await f.page.locator('#setup-title').innerText(),purpose==='invite'?/Create/:/Choose/);
      await f.page.locator('#redeem-form [name=password]').fill('Synthetic-new-phrase!');await f.page.locator('#redeem-form [name=password_confirmation]').fill('Synthetic-new-phrase!');
      await f.page.locator('#redeem-submit').click();await f.page.locator('#success-section').waitFor({state:'visible'});
      assert.match(await f.page.locator('#success-title').innerText(),purpose==='invite'?/account is ready/:/reset successfully/);
      if(process.env.KNOWVIA_ACCOUNT_UX_SCREENSHOT_DIR)await f.page.screenshot({path:path.join(process.env.KNOWVIA_ACCOUNT_UX_SCREENSHOT_DIR,`${purpose}-success-390.png`),fullPage:true});
      assert.equal(f.posts.length,1);assert(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await f.context.close();
    }
    f=await fixture(browser,'https://knowvia.test/password#purpose=verify&token='+token);
    await f.page.locator('#redeem-submit').click();
    await f.page.locator('#redeem-form').waitFor({state:'hidden'});
    assert.deepEqual(Object.keys(f.posts[0].body).sort(),['purpose','token']);
    await f.context.close();
    for(const purpose of ['invite','reset']){
      for(const width of [360,1280]){
        f=await fixture(browser,'https://knowvia.test/password#purpose='+purpose+'&token=invalid',width);
        assert.match(await f.page.locator('#feedback').innerText(),/invalid, expired, or has already been used/);
        assert.equal(await f.page.locator('#redeem-form').isVisible(),false);assert.equal(f.posts.length,0);
        assert.equal(await f.page.locator('#new-reset-link').isVisible(),purpose==='reset');
        if(purpose==='invite')assert.match(await f.page.locator('#link-help-copy').innerText(),/administrator/);
        assert(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await f.context.close();
      }
      f=await fixture(browser,'https://knowvia.test/password#purpose='+purpose+'&token='+token,390,undefined,true);
      await f.page.locator('#redeem-form [name=password]').fill('short');await f.page.locator('#redeem-form [name=password_confirmation]').fill('short');await f.page.locator('#redeem-submit').click();
      assert.equal(await f.page.locator('#feedback').innerText(),'Your password must contain at least 12 characters.');assert.equal(f.posts.length,0);
      await f.page.locator('#redeem-form [name=password]').fill('Synthetic-new-phrase!');await f.page.locator('#redeem-form [name=password_confirmation]').fill('Different-phrase!');await f.page.locator('#redeem-submit').click();
      assert.equal(await f.page.locator('#feedback').innerText(),'The passwords do not match. Please try again.');assert.equal(f.posts.length,0);
      await f.page.locator('#redeem-form [name=password_confirmation]').fill('Synthetic-new-phrase!');await f.page.locator('#redeem-submit').click();
      await f.page.locator('#link-help').waitFor({state:'visible'});
      assert.match(await f.page.locator('#feedback').innerText(),/does not identify the exact cause/);
      assert.equal(f.posts.length,1);await f.context.close();
    }
    f=await fixture(browser,'https://knowvia.test/password',390);
    await f.page.locator('#forgot-form [name=work_email]').fill('employee@example.org');
    await f.page.locator('#forgot-form button').click();
    await f.page.waitForFunction(()=>document.querySelector('#feedback').textContent==='Generic reset response.');
    assert.equal(f.posts.length,1);
    assert(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await f.context.close();
    f=await fixture(browser,'https://knowvia.test/account');
    await f.page.locator('#account-section').waitFor({state:'visible'});
    await f.page.locator('#change-form [name=current_password]').fill('Synthetic-old-phrase!');
    await f.page.locator('#change-form [name=password]').fill('Synthetic-new-phrase!');
    await f.page.locator('#change-form [name=password_confirmation]').fill('Synthetic-new-phrase!');
    await f.page.locator('#change-form button').click();
    await f.page.locator('#account-section').waitFor({state:'hidden'});
    assert.equal(f.posts.length,1);
    assert.equal(await f.page.locator('#change-form [name=current_password]').inputValue(),'');
    await f.context.close();
    for(const width of [360,768,1280]){
      f=await fixture(browser,'https://knowvia.test/account',width,{email_verified:false,email_delivery_enabled:false},true);
      await f.page.locator('#account-section').waitFor({state:'visible'});
      assert.equal(await f.page.locator('#email-badge').innerText(),'Not verified');
      assert.equal(await f.page.locator('#mail-state').innerText(),'Disabled');
      assert.equal(await f.page.locator('#mail-notice').isVisible(),true);
      assert.equal(await f.page.locator('#feedback').innerText(),'');
      assert(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      const formBounds=await f.page.locator('#change-form').boundingBox(),buttonBounds=await f.page.locator('#change-form button').boundingBox();
      assert(formBounds.width<=540.5,'Change Password form remains compact');
      const currentBounds=await f.page.locator('#change-form [name=current_password]').boundingBox();
      const overviewBounds=await f.page.locator('.overview').boundingBox();
      const changeSectionBounds=await f.page.locator('.settings-sections>.card').first().boundingBox();
      const recoveryBounds=await f.page.locator('.recovery').boundingBox();
      assert(changeSectionBounds.y>=overviewBounds.y+overviewBounds.height-1,'Change Password follows Security overview');
      if(width>760){
        assert(recoveryBounds.x>=changeSectionBounds.x+changeSectionBounds.width-1,'Recovery Email stays beside Change Password');
      }else{
        assert(recoveryBounds.y>=changeSectionBounds.y+changeSectionBounds.height-1,'Sections stack on mobile');
      }
      const newBounds=await f.page.locator('#change-form [name=password]').boundingBox();
      const confirmBounds=await f.page.locator('#change-form [name=password_confirmation]').boundingBox();
      assert(Math.abs(currentBounds.width-formBounds.width)<2,'Current Password fills the compact form');
      assert.equal(currentBounds.height,newBounds.height);assert.equal(newBounds.height,confirmBounds.height);
      if(width>520){assert(Math.abs(newBounds.y-confirmBounds.y)<2);assert(confirmBounds.x>newBounds.x);}
      else{assert(confirmBounds.y>newBounds.y);assert(Math.abs(newBounds.width-formBounds.width)<2);}
      assert(Math.abs(currentBounds.x-buttonBounds.x)<2,'Password action aligns to form input left edge');
      const hintBounds=await f.page.locator('#change-hint').boundingBox();
      assert(buttonBounds.y>=hintBounds.y+hintBounds.height && buttonBounds.y-hintBounds.y-hintBounds.height<=20,'Action follows password guidance without excessive whitespace');
      assert.equal(await f.page.locator('#change-form .form-actions').evaluate(e=>getComputedStyle(e).paddingTop),'2px');
      if(process.env.KNOWVIA_CONTROL_SCREENSHOT_DIR){fs.mkdirSync(process.env.KNOWVIA_CONTROL_SCREENSHOT_DIR,{recursive:true});await f.page.screenshot({path:path.join(process.env.KNOWVIA_CONTROL_SCREENSHOT_DIR,`password-alignment-${width}.png`),fullPage:true});}
      if(width===1280){
        assert((await f.page.locator('.security-shell').boundingBox()).width<=1040);
        assert(parseFloat(await f.page.locator('#title').evaluate(e=>getComputedStyle(e).fontSize))<=24);
        assert((await f.page.locator('.overview').boundingBox()).height<180);
        assert(Math.abs(formBounds.width-540)<1,'Desktop form uses its 540px maximum');
      }
      const input=f.page.locator('#change-form [name=current_password]');
      await input.focus();await f.page.keyboard.press('Tab');
      assert.equal(await f.page.locator('#change-form [name=password]').evaluate(e=>e===document.activeElement),true);
      assert.notEqual(await f.page.locator('#change-form [name=password]').evaluate(e=>getComputedStyle(e).outlineStyle),'none');
      await input.fill('Synthetic-old-phrase!');
      await f.page.locator('#change-form [name=password]').fill('Synthetic-new-phrase!');
      await f.page.locator('#change-form [name=password_confirmation]').fill('Synthetic-new-phrase!');
      await f.page.locator('#change-form button').click();
      await f.page.waitForFunction(()=>document.querySelector('#feedback').className==='error');
      assert.equal(await f.page.locator('#mail-notice').isVisible(),true);
      assert.equal(await input.inputValue(),'');assert.equal(f.posts.length,1);
      if(process.env.KNOWVIA_SECURITY_SCREENSHOT_DIR){fs.mkdirSync(process.env.KNOWVIA_SECURITY_SCREENSHOT_DIR,{recursive:true});await f.page.screenshot({path:path.join(process.env.KNOWVIA_SECURITY_SCREENSHOT_DIR,`security-${width}.png`),fullPage:true});}
      await f.context.close();
    }
    f=await fixture(browser,'https://knowvia.test/account',1100,{email_verified:true,email_delivery_enabled:true});
    await f.page.locator('#account-section').waitFor({state:'visible'});
    assert.equal(await f.page.locator('#email-badge').innerText(),'Verified');
    assert.equal(await f.page.locator('#mail-notice').isVisible(),false);
    await f.context.close();
    console.log('Password UI behavioral checks: PASS');
  }finally{await browser.close();}
})().catch(error=>{console.error('Password UI behavioral check failed. No credentials or tokens are printed.');console.error((error.stack||'').split('\n').filter(line=>line.trim().startsWith('at ')).join('\n'));process.exit(1);});
