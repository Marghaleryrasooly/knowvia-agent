/* Real disposable login/session/API; conversation service is synthetic. No production requests. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.KNOWVIA_TEST_BASE_URL,shots=process.env.KNOWVIA_PROFILE_SCREENSHOTS;
assert.equal(new URL(base).hostname,'127.0.0.1');
const password='Synthetic-test-password!';
async function shot(page,name){if(shots){fs.mkdirSync(shots,{recursive:true});await page.screenshot({path:path.join(shots,name+'.png'),fullPage:true});}}
async function resolved(page,value){await page.waitForFunction(v=>document.documentElement.dataset.theme===v,value);}
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});let checks=0;
 try{
  for(const [width,height] of [[390,700],[1280,800],[390,380]]){
   const context=await browser.newContext({viewport:{width,height},colorScheme:'dark',reducedMotion:'reduce'}),page=await context.newPage(),errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   page.on('console',m=>{if(m.type()==='error'&&!m.text().startsWith('Failed to load resource:'))errors.push(m.text());});
   page.on('response',r=>{if(new URL(r.url()).pathname.startsWith('/static/')&&r.status()>=400)errors.push('Broken static asset');});
   await context.route('**/*',route=>{
    const url=new URL(route.request().url());
    if(url.origin!==base)return route.abort();
    if(process.env.KNOWVIA_PROFILE_PROBE_MAIN&&(url.pathname==='/'||url.pathname.startsWith('/static/'))){
     const name=url.pathname==='/'?'index.html':path.basename(url.pathname);
     return route.fulfill({contentType:name.endsWith('.html')?'text/html':name.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(path.join(process.env.KNOWVIA_PROFILE_PROBE_MAIN,name),'utf8')});
    }
    if(process.env.KNOWVIA_PROFILE_BASELINE&&url.pathname==='/static/theme.css')return route.fulfill({contentType:'text/css',body:fs.readFileSync(process.env.KNOWVIA_PROFILE_BASELINE,'utf8')});
    assert(!/\/api\/(chat|proposals)/.test(url.pathname),'No model or action requests permitted');
    return route.continue();
   });
   await page.goto(base+'/');await page.locator('#auth-content').waitFor({state:'visible'});
   if(!process.env.KNOWVIA_PROFILE_PROBE_MAIN)assert.equal(await page.locator('.appearance-profile [role=radio]').count(),3,'Profile options are pre-rendered before login');
   await page.locator('#signin-email').fill('profile-employee@novatech.example');await page.locator('#signin-password').fill(password);
   await page.locator('#signin-submit').click();await page.locator('#app-shell').waitFor({state:'visible'});
   assert.equal((await context.request.get(base+'/api/auth/me')).status(),200);
   if(width<781)await page.locator('#mobile-menu').click();
   await page.locator('[data-chat="profile-rendering"]').click();await page.locator('.answer-markdown').waitFor({state:'visible'});
   await page.locator('#profile-trigger').click();
   const menu=page.locator('#profile-popover'),select=menu.getByRole('radiogroup',{name:'Appearance'});
   const selectedValue=()=>select.getByRole('radio',{checked:true}).getAttribute('data-appearance-value');
   if(process.env.KNOWVIA_PROFILE_PROBE_MAIN){
    await shot(page,'deployed-profile-'+width+'x'+height);
    console.log(JSON.stringify({width,height,appearanceCount:await menu.getByRole('combobox',{name:'Appearance'}).count(),background:await menu.evaluate(e=>getComputedStyle(e).backgroundColor)}));
    await context.close();checks++;continue;
   }
   for(const preference of ['light','dark','system']){
    await select.locator('[data-appearance-value="'+preference+'"]').click();await resolved(page,preference==='system'?'dark':preference);
    assert(await menu.isVisible());assert(await select.isVisible());assert.equal(await select.count(),1);
    assert.equal(await selectedValue(),preference);
    const buttons=await select.getByRole('radio').all(),boxes=await Promise.all(buttons.map(b=>b.boundingBox()));
    assert(boxes.every(b=>Math.abs(b.y-boxes[0].y)<1),'All three options share one row');
    assert(await select.evaluate(e=>e.scrollWidth<=e.clientWidth),'Segmented control fits profile');
    assert.equal(await menu.evaluate(e=>e.querySelector('.profile-security-action').compareDocumentPosition(e.querySelector('.appearance-profile'))&Node.DOCUMENT_POSITION_FOLLOWING),4);
    assert.equal(await menu.evaluate(e=>e.querySelector('.appearance-profile').compareDocumentPosition(e.querySelector('#signout-button'))&Node.DOCUMENT_POSITION_FOLLOWING),4);
    await select.scrollIntoViewIfNeeded();await page.waitForTimeout(250);
    await shot(page,'profile-'+preference+'-'+width+'x'+height);
    const background=await menu.evaluate(e=>getComputedStyle(e).backgroundColor);
    assert.equal(background,preference==='light'?'rgb(255, 255, 255)':'rgb(24, 28, 33)','Profile must be fully opaque');
    const occluded=await menu.evaluate(e=>{
     const r=e.getBoundingClientRect();let count=0;
     for(let x=r.left+14;x<r.right-14;x+=30)for(let y=r.top+14;y<Math.min(r.bottom,innerHeight)-14;y+=25){if(!e.contains(document.elementFromPoint(x,y)))count++;}
     return count;
    });assert.equal(occluded,0,'All sampled menu points paint above chat content');
    // Pixel comparison: removing underlying real-rendered chat must not change the card.
    const rect=await menu.boundingBox(),clip={x:rect.x+12,y:rect.y+12,width:rect.width-24,height:rect.height-24};
    const visible=await page.screenshot({clip});const hide=await page.addStyleTag({content:'.messages{visibility:hidden!important}'});
    const hidden=await page.screenshot({clip});await hide.evaluate(e=>e.remove());assert(visible.equals(hidden),'Underlying chat must not change profile pixels');
    checks++;
   }
   const selected=select.getByRole('radio',{checked:true});await selected.focus();await page.keyboard.press('Tab');await page.keyboard.press('Shift+Tab');
   assert.equal(await selected.evaluate(e=>e===document.activeElement),true);assert.notEqual(await selected.evaluate(e=>getComputedStyle(e).outlineStyle),'none');
   await page.emulateMedia({colorScheme:'light'});await resolved(page,'light');assert.equal(await selectedValue(),'system');
   await page.emulateMedia({colorScheme:'dark'});await resolved(page,'dark');
   await menu.locator('.profile-security-action').click();await page.locator('#account-section').waitFor({state:'visible'});assert.equal(await page.getByRole('radiogroup',{name:'Appearance'}).getByRole('radio',{checked:true}).getAttribute('data-appearance-value'),'system');
   await page.goto(base+'/');await page.locator('#app-shell').waitFor({state:'visible'});await page.locator('#profile-trigger').click();assert.equal(await selectedValue(),'system');
   await page.reload();await page.locator('#app-shell').waitFor({state:'visible'});await page.locator('#profile-trigger').click();assert.equal(await selectedValue(),'system');
   await page.locator('#signout-button').click();await page.locator('#auth-content').waitFor({state:'visible'});assert.equal((await context.request.get(base+'/api/auth/me')).status(),401);
   assert.equal(await page.evaluate(()=>localStorage.getItem('knowvia.appearance')),'system');
   assert.equal(await page.locator('[data-appearance-control]:visible').count(),0);
   assert.equal(await page.evaluate(()=>document.documentElement.dataset.appearance),'system');
   await page.reload();await page.locator('#auth-content').waitFor({state:'visible'});await resolved(page,'dark');
   assert.equal(await page.locator('[data-appearance-control]:visible').count(),0);
   assert.equal(await page.evaluate(()=>document.documentElement.dataset.appearance),'system');
   assert.deepEqual(errors,[]);await context.close();checks++;
  }
  if(process.env.KNOWVIA_PROFILE_PROBE_MAIN){console.log('PASS: current deployed assets probed with real disposable authentication.');return;}
  const context=await browser.newContext(),page=await context.newPage();await context.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  await page.goto(base+'/admin');await page.locator('#login-section').waitFor({state:'visible'});
  await page.locator('#login-form [name=work_email]').fill('profile-admin@novatech.example');await page.locator('#login-form [name=password]').fill(password);await page.locator('#login-form button[type=submit]').click();await page.locator('#dashboard').waitFor({state:'visible'});
  for(const mode of ['light','dark']){
   const group=page.locator('.appearance-sidebar').getByRole('radiogroup',{name:'Appearance'}),button=group.locator('[data-appearance-value="'+mode+'"]');await button.click();await resolved(page,mode);
   assert.equal(await button.getAttribute('aria-checked'),'true');assert.equal(await button.evaluate(e=>getComputedStyle(e).height),'36px');
   await button.focus();await page.keyboard.press('ArrowDown');await button.click();await resolved(page,mode);await page.waitForTimeout(250);
   await button.focus();await page.keyboard.press('ArrowRight');await page.keyboard.press('ArrowLeft');
   assert.notEqual(await button.evaluate(e=>getComputedStyle(e).outlineStyle),'none');
   await shot(page,'admin-native-'+mode);checks++;
  }
  await context.close();console.log('PASS '+checks+' real-login profile/segmented-control scenarios with pixel occlusion checks.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
