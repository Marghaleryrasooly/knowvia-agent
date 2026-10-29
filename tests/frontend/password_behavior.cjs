/* Every request is intercepted. Tokens/passwords below are synthetic fixtures. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const assets=path.resolve(__dirname,'../../src/knowvia/static');
async function fixture(browser,url,width=1100,status={email_verified:false,email_delivery_enabled:true},failure=false){
  const context=await browser.newContext({viewport:{width,height:900}});
  const page=await context.newPage(), posts=[];
  await page.route('**/*',route=>{
    const request=route.request(),parsed=new URL(request.url());
    assert.equal(parsed.hostname,'knowvia.test');
    if(['/password','/account'].includes(parsed.pathname))return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(assets,'password.html'),'utf8')});
    if(parsed.pathname.startsWith('/static/'))return route.fulfill({contentType:parsed.pathname.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(path.join(assets,path.basename(parsed.pathname)),'utf8')});
    if(request.method()==='POST'){
      posts.push({path:parsed.pathname,body:request.postDataJSON()});
      if(failure)return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'The request could not be completed.'})});
      return route.fulfill({contentType:'application/json',body:JSON.stringify({completed:true,changed:true,sent:true,message:'Generic reset response.'})});
    }
    return route.fulfill({contentType:'application/json',body:JSON.stringify(status)});
  });
  await page.goto(url);
  return {context,page,posts};
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
    await f.context.close();
    f=await fixture(browser,'https://knowvia.test/password#purpose=verify&token='+token);
    await f.page.locator('#redeem-submit').click();
    await f.page.locator('#redeem-form').waitFor({state:'hidden'});
    assert.deepEqual(Object.keys(f.posts[0].body).sort(),['purpose','token']);
    await f.context.close();
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
      if(width===1280){
        assert((await f.page.locator('.security-shell').boundingBox()).width<=1040);
        assert.equal(await f.page.locator('.overview').evaluate(e=>getComputedStyle(e).paddingTop),'20px');
        assert.equal(await f.page.locator('.settings-sections').evaluate(e=>getComputedStyle(e).rowGap),'16px');
        assert((await f.page.locator('.overview').boundingBox()).height<520);
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
})().catch(()=>{console.error('Password UI behavioral check failed. No credentials or tokens are printed.');process.exit(1);});
