/* Every request is intercepted. Tokens/passwords below are synthetic fixtures. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const assets=path.resolve(__dirname,'../../src/knowvia/static');
async function fixture(browser,url,width=1100){
  const context=await browser.newContext({viewport:{width,height:900}});
  const page=await context.newPage(), posts=[];
  await page.route('**/*',route=>{
    const request=route.request(),parsed=new URL(request.url());
    assert.equal(parsed.hostname,'knowvia.test');
    if(['/password','/account'].includes(parsed.pathname))return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(assets,'password.html'),'utf8')});
    if(parsed.pathname.startsWith('/static/'))return route.fulfill({contentType:parsed.pathname.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(path.join(assets,path.basename(parsed.pathname)),'utf8')});
    if(request.method()==='POST'){
      posts.push({path:parsed.pathname,body:request.postDataJSON()});
      return route.fulfill({contentType:'application/json',body:JSON.stringify({completed:true,changed:true,sent:true,message:'Generic reset response.'})});
    }
    return route.fulfill({contentType:'application/json',body:JSON.stringify({email_verified:false,email_delivery_enabled:true})});
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
    console.log('Password UI behavioral checks: PASS');
  }finally{await browser.close();}
})().catch(()=>{console.error('Password UI behavioral check failed. No credentials or tokens are printed.');process.exit(1);});
