/* Offline appearance fixtures: every network request is intercepted. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const assets=path.resolve(__dirname,'../../src/knowvia/static');
const screenshots=process.env.KNOWVIA_THEME_SCREENSHOTS;
const employee={employee_id:'appearance-demo',display_name:'Demo Employee',work_email:'demo@novatech.example',department:'IT',role:'Employee'};
function appearance(page, scope='[data-appearance-control]:visible') {
 const host=page.locator(scope).first(),group=host.getByRole('radiogroup',{name:'Appearance'});
 const current=()=>group.getByRole('radio',{checked:true});
 return {selectOption:value=>group.locator('[data-appearance-value="'+value+'"]').click(),
 inputValue:()=>current().getAttribute('data-appearance-value'),labels:()=>group.getByRole('radio').allTextContents(),
 isVisible:()=>group.isVisible(),boundingBox:()=>current().boundingBox(),focus:()=>current().focus(),evaluate:fn=>current().evaluate(fn)};
}
async function fixture(browser,{width=1280,authenticated=false,preference,broken=false,baseline=false,storageState,registration=false}={}){
  const context=await browser.newContext({viewport:{width,height:900},colorScheme:'dark',reducedMotion:'reduce',storageState});
  if(preference!==undefined)await context.addInitScript(value=>localStorage.setItem('knowvia.appearance',value),preference);
  if(broken)await context.addInitScript(()=>Object.defineProperty(window,'localStorage',{get(){throw new Error('Storage blocked');}}));
  const state={authenticated,posts:[]};
  await context.route('**/*',async route=>{
    const url=new URL(route.request().url()),p=url.pathname;
    if(url.hostname!=='knowvia.test')return route.abort();
    if(route.request().method()==='POST')state.posts.push(p);
    const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    const directory=baseline?process.env.KNOWVIA_THEME_BASELINE:assets;
    if(['/','/admin','/password','/account'].includes(p))return route.fulfill({contentType:'text/html',headers:{'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'"},body:fs.readFileSync(path.join(directory,p==='/'?'index.html':p==='/admin'?'admin.html':'password.html'),'utf8')});
    if(p.startsWith('/static/'))return route.fulfill({contentType:p.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(path.join(directory,path.basename(p)),'utf8')});
    if(p==='/api/config')return json({registration_enabled:registration,development_identity_override:false,developer_details_enabled:false});
    if(p==='/api/auth/me')return json(state.authenticated?{employee}:{error:'Authentication required'},state.authenticated?200:401);
    if(p==='/api/auth/logout'){state.authenticated=false;return json({logged_out:true});}
    if(p==='/api/admin/me')return json({administrator:true,employee_id:'admin'},state.authenticated?200:401);
    if(p==='/api/admin/roles')return json({roles:[{name:'employee',permissions:[]},{name:'it-reader',permissions:['knowledge.it.read']}]});
    if(p==='/api/admin/accounts')return json({accounts:[{...employee,first_name:'Demo',last_name:'Employee',active:true,account_status:'ACTIVE',roles:['employee','it-reader']}]});
    if(p==='/api/admin/knowledge')return json({active_version:'reference',revision:'clean',staged:false,rollback_available:false,documents:[]});
    if(p==='/api/admin/audit')return json({events:[]});
    if(p==='/api/auth/password/status')return json({email_verified:false,email_delivery_enabled:false});
    if(p==='/api/auth/password/redeem'&&route.request().method()==='POST')return json({completed:true});
    if(p==='/api/conversations')return json({conversations:[]});
    if(p==='/api/notifications')return json({notifications:[]});
    throw new Error('Unexpected offline request: '+p);
  });
  const page=await context.newPage();page.on('pageerror',error=>{throw error;});return {context,page,state};
}
async function theme(page,value){await page.waitForFunction(v=>document.documentElement.dataset.theme===v,value);}
async function capture(page,name){if(screenshots){fs.mkdirSync(screenshots,{recursive:true});await page.screenshot({path:path.join(screenshots,name+'.png'),fullPage:true});}}
async function noOverflow(page){assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Page overflows viewport');}
function contrast(a,b){
  const luminance=color=>{const rgb=color.match(/\d+/g).slice(0,3).map(Number).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;});return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;};
  const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);
}
(async()=>{
  let browser;try{browser=await chromium.launch({channel:'msedge',headless:true});}catch(_){try{browser=await chromium.launch({channel:'chrome',headless:true});}catch(_){process.exit(77);}}
  let checks=0;
  try{
    for(const width of [390,1280])for(const preference of ['light','dark','system']){
      const t=await fixture(browser,{width,authenticated:true,preference});await t.page.goto('http://knowvia.test/admin');
      await t.page.locator('#dashboard').waitFor({state:'visible'});
      if(width<761)await t.page.locator('#menu-toggle').click();
      await t.page.locator('#admin-navigation [data-view="employees"]').click();
      const row=t.page.locator('.employee-table tbody tr').first();await row.hover();
      const styles=await row.evaluate(e=>({background:getComputedStyle(e).backgroundColor,display:getComputedStyle(e).display,lastCellPosition:getComputedStyle(e.lastElementChild).position}));
      assert.equal(styles.background,preference==='light'?'rgb(250, 252, 253)':'rgb(29, 35, 42)','Hovered '+preference+' row at '+width+'px: '+JSON.stringify(styles));
      assert.equal(styles.display,width<761?'block':'table-row');
      if(width>=761)assert.equal(styles.lastCellPosition,'sticky');
      if(preference==='system'){
        await t.page.emulateMedia({colorScheme:'light'});await theme(t.page,'light');await row.hover();
        assert.equal(await row.evaluate(e=>getComputedStyle(e).backgroundColor),'rgb(250, 252, 253)');
      }
      await capture(t.page,'table-hover-'+preference+'-'+width);await t.context.close();checks++;
    }
    if(process.env.KNOWVIA_THEME_TABLE_ONLY){console.log('PASS '+checks+' targeted table-hover checks.');return;}
    for(const width of [390,768,1024,1280]){
      const employeeView=await fixture(browser,{width,authenticated:true});
      const p=employeeView.page;await p.goto('http://knowvia.test/');await p.locator('#app-shell').waitFor({state:'visible'});
      await p.evaluate(()=>{window.appearanceDocumentMarker='same-authenticated-document';});
      await p.locator('#profile-trigger').click();
      const control=p.getByRole('radiogroup',{name:'Appearance'});
      assert.equal(await p.locator('#app-shell [data-appearance-value]').count(),3,'Exactly three profile options');
      for(const preference of ['dark','light','system']){
        await control.getByRole('radio',{checked:true}).focus();await p.keyboard.press('Home');
        for(let i=0;i<['light','dark','system'].indexOf(preference);i++)await p.keyboard.press('ArrowDown');
        await p.keyboard.press('Enter');
        await theme(p,preference==='system'?'dark':preference);
        assert.equal(await control.getByRole('radio',{checked:true}).getAttribute('data-appearance-value'),preference);assert(await p.locator('#profile-popover').isVisible());
        assert.equal(await p.locator('#profile-trigger').getAttribute('aria-expanded'),'true');
        assert.equal(await p.evaluate(()=>window.appearanceDocumentMarker),'same-authenticated-document');
        assert.equal(employeeView.state.authenticated,true);assert.deepEqual(employeeView.state.posts,[]);
        checks++;
      }
      await control.getByRole('radio',{checked:true}).focus();
      for(const [key,value] of [['ArrowRight','light'],['Space','light'],['ArrowLeft','system'],['Home','light'],['End','system'],['ArrowUp','dark'],['ArrowDown','system']]){
        await p.keyboard.press(key);
        assert.equal(await control.getByRole('radio',{checked:true}).getAttribute('data-appearance-value'),value);
        assert(await p.locator('#profile-popover').isVisible());
      }
      checks++;
      const appearanceTab=await employeeView.context.newPage();await appearanceTab.goto('http://knowvia.test/password');
      const otherControl=appearance(appearanceTab);
      for(const value of ['dark','system']){
        await otherControl.selectOption(value);
        await p.waitForFunction(v=>document.querySelector('.appearance-profile [aria-checked=true]').dataset.appearanceValue===v,value);
        assert(await p.locator('#profile-popover').isVisible());
      }
      await appearanceTab.close();checks++;
      const selected=control.getByRole('radio',{checked:true});await selected.focus();await p.keyboard.press('Tab');await p.keyboard.press('Shift+Tab');
      assert.equal(await selected.evaluate(e=>e===document.activeElement),true);
      assert.notEqual(await selected.evaluate(e=>getComputedStyle(e).outlineStyle),'none');
      await p.emulateMedia({colorScheme:'light'});await theme(p,'light');assert.equal(await selected.getAttribute('data-appearance-value'),'system');
      await p.emulateMedia({colorScheme:'dark'});await theme(p,'dark');
      await capture(p,'signed-in-profile-'+width);
      await p.locator('.profile-security-action').click();await p.locator('#account-section').waitFor({state:'visible'});await theme(p,'dark');
      assert.equal(await appearance(p).inputValue(),'system');
      await p.goto('http://knowvia.test/');await p.locator('#app-shell').waitFor({state:'visible'});await p.locator('#profile-trigger').click();
      assert.equal(await p.locator('.appearance-profile [aria-checked=true]').getAttribute('data-appearance-value'),'system');
      await p.reload();await p.locator('#app-shell').waitFor({state:'visible'});await theme(p,'dark');
      assert.deepEqual(employeeView.state.posts,[]);await noOverflow(p);await employeeView.context.close();checks++;

      const adminView=await fixture(browser,{width,authenticated:true});const a=adminView.page;
      await a.goto('http://knowvia.test/admin');await a.locator('#dashboard').waitFor({state:'visible'});
      await a.evaluate(()=>{window.appearanceDocumentMarker='same-admin-document';});
      for(const section of ['overview','employees','add','knowledge','activity']){
        if(width<761&&await a.locator('#menu-toggle').getAttribute('aria-expanded')!=='true')await a.locator('#menu-toggle').click();
        await a.locator('#admin-navigation [data-view="'+section+'"]').click();
        if(width<761)await a.locator('#menu-toggle').click();
        const selector=appearance(a,'.appearance-sidebar');assert(await selector.isVisible());
        for(const preference of ['light','dark','system']){
          await selector.selectOption(preference);await theme(a,preference==='system'?'dark':preference);
          assert.equal(await selector.inputValue(),preference);assert(await a.locator('[data-panel="'+section+'"]').isVisible());
          assert.equal(await a.evaluate(()=>window.appearanceDocumentMarker),'same-admin-document');
          assert.deepEqual(adminView.state.posts,[]);assert.equal(adminView.state.authenticated,true);checks++;
          if(preference==='dark'&&[390,1280].includes(width))await capture(a,'admin-'+section+'-'+width);
        }
      }
      await a.emulateMedia({colorScheme:'light'});await theme(a,'light');
      assert.equal(await appearance(a,'.appearance-sidebar').inputValue(),'system');
      await a.emulateMedia({colorScheme:'dark'});await theme(a,'dark');await capture(a,'signed-in-admin-menu-'+width);
      await a.reload();await a.locator('#dashboard').waitFor({state:'visible'});await theme(a,'dark');
      if(width<761)await a.locator('#menu-toggle').click();
      assert.equal(await appearance(a,'.appearance-sidebar').inputValue(),'system');assert.deepEqual(adminView.state.posts,[]);
      await noOverflow(a);await adminView.context.close();checks++;
    }
    if(process.env.KNOWVIA_THEME_SIGNED_IN_ONLY){console.log('PASS '+checks+' signed-in appearance checks; no mutation requests or external connections.');return;}
    // Sign-in has no Appearance control; saved themes still apply before login.
    for(const width of [390,768,1024,1280])for(const preference of ['light','dark','system'])for(const [route,authenticated] of [['/admin',false],['/admin',true],['/',false]]){
      const h=await fixture(browser,{width,preference,authenticated});await h.page.goto('http://knowvia.test'+route);
      await h.page.locator(route==='/'?'#auth-content':authenticated?'#dashboard':'#login-section').waitFor({state:'visible'});
      await theme(h.page,preference==='system'?'dark':preference);
      assert.equal(await h.page.evaluate(()=>document.documentElement.dataset.appearance),preference);
      if(authenticated){
        if(width<761)await h.page.locator('#menu-toggle').click();
        assert(await h.page.locator('.appearance-sidebar').isVisible());
        assert.equal(await appearance(h.page,'.appearance-sidebar').inputValue(),preference);
      }else{
        assert.equal(await h.page.locator('[data-appearance-control]:visible').count(),0,'No login Appearance control');
        const card=h.page.locator(route==='/'?'.auth-card':'#login-section'),box=await card.boundingBox();
        assert(Math.abs(box.x+box.width/2-width/2)<=2,'Sign-in card stays centered');
        const input=h.page.locator(route==='/'?'#signin-email':'#login-form [name=work_email]');
        await input.focus();await h.page.keyboard.press('Tab');
        assert(await h.page.evaluate(()=>document.activeElement?.tagName==='INPUT'),'Keyboard reaches password field');
        if(route==='/admin'){
          const header=await h.page.locator('.header').boundingBox();
          assert(box.y>=header.y+header.height,'No sign-in/header overlap');
          for(const link of await h.page.locator('.header nav a').all())
            assert(await link.evaluate(e=>{const b=e.getBoundingClientRect();return e.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2));}),'Navigation clickable');
        }
      }
      await noOverflow(h.page);
      if(route==='/admin'&&!authenticated)await capture(h.page,'admin-signin-'+preference+'-'+width);
      if(route==='/')await capture(h.page,'employee-signin-'+preference+'-'+width);
      await h.context.close();checks++;
    }
    if(process.env.KNOWVIA_THEME_HEADERS_ONLY){console.log('PASS '+checks+' responsive header checks; all requests intercepted.');return;}
    for(const width of [390,768,1024,1280])for(const route of ['/','/admin']){
      const t=await fixture(browser,{width});await t.page.goto('http://knowvia.test'+route);
      await t.page.locator(route==='/'?'#auth-content':'#login-section').waitFor({state:'visible'});
      await theme(t.page,'light');
      assert.equal(await t.page.evaluate(()=>localStorage.getItem('knowvia.appearance')),null,'No first-visit preference is invented');
      assert.equal(await t.page.locator('[data-appearance-control]:visible').count(),0);
      await noOverflow(t.page);await t.context.close();checks++;
    }
    for(const width of [390,768,1024,1280])for(const preference of ['light','dark','system']){
      const t=await fixture(browser,{width,authenticated:true,preference});await t.page.goto('http://knowvia.test/account');
      await t.page.locator('#account-section').waitFor({state:'visible'});
      const group=t.page.getByRole('radiogroup',{name:'Appearance'}),selected=group.getByRole('radio',{checked:true});
      assert.equal(await group.count(),1);assert.equal(await selected.getAttribute('data-appearance-value'),preference);
      const colors=await selected.evaluate(e=>{const s=getComputedStyle(e);return [s.color,s.backgroundColor];});
      assert(contrast(...colors)>=4.5,'Selected appearance text meets normal-text contrast');
      assert(await group.evaluate(e=>e.scrollWidth<=e.clientWidth));await noOverflow(t.page);
      if([390,1280].includes(width))await capture(t.page,'account-'+preference+'-'+width);
      await t.context.close();checks++;
    }
    for(const width of [390,768,1024,1280])for(const registration of [false,true]){
      const t=await fixture(browser,{width,registration});await t.page.goto('http://knowvia.test/');
      await t.page.locator('#auth-content').waitFor({state:'visible'});
      assert.equal(await t.page.locator('.auth-tabs').isVisible(),registration);
      assert.equal(await t.page.locator('#auth-title').innerText(),'Welcome back');
      assert(await t.page.locator('#signin-form').isVisible());
      if(registration){
        await t.page.locator('#register-tab').click();assert(await t.page.locator('#register-form').isVisible());
        await t.page.locator('#signin-tab').click();assert(await t.page.locator('#signin-form').isVisible());
      }
      const box=await t.page.locator('.auth-card').boundingBox();
      assert(Math.abs(box.x+box.width/2-width/2)<=2,'Employee sign-in stays horizontally centered');
      await noOverflow(t.page);assert.deepEqual(t.state.posts,[]);await t.context.close();checks++;
    }
    let f=await fixture(browser);await f.page.goto('http://knowvia.test/password');await theme(f.page,'light');checks++;
    await appearance(f.page).selectOption('dark');
    const returningStorage=await f.context.storageState();await f.context.close();
    f=await fixture(browser,{storageState:returningStorage});await f.page.goto('http://knowvia.test/password');await theme(f.page,'dark');
    await appearance(f.page).selectOption('light');checks++;
    const select=appearance(f.page);
    assert.deepEqual(await select.labels(),['Light','Dark','System']);
    await select.selectOption('system');await theme(f.page,'dark');
    await f.page.emulateMedia({colorScheme:'light'});await theme(f.page,'light');
    assert.equal(await select.inputValue(),'system');await f.page.reload();await theme(f.page,'light');assert.equal(await select.inputValue(),'system');
    await select.selectOption('dark');await f.page.emulateMedia({colorScheme:'light'});await theme(f.page,'dark');
    await f.page.reload();await theme(f.page,'dark');checks+=4;
    await select.focus();await f.page.keyboard.press('Home');await f.page.keyboard.press('Enter');await theme(f.page,'light');
    assert.notEqual(await select.evaluate(e=>getComputedStyle(e).outlineStyle),'none');checks++;
    const second=await f.context.newPage();await second.goto('http://knowvia.test/admin');await theme(second,'light');
    await select.selectOption('dark');await theme(second,'dark');
    await f.page.goto('http://knowvia.test/');await theme(f.page,'dark');
    await f.page.locator('#auth-content').waitFor({state:'visible'});
    assert.equal(await f.page.locator('[data-appearance-control]:visible').count(),0);
    await second.evaluate(()=>localStorage.removeItem('knowvia.appearance'));await theme(f.page,'light');checks+=3;await f.context.close();
    for(const preference of ['garbage','null']){f=await fixture(browser,{preference});await f.page.goto('http://knowvia.test/password');await theme(f.page,'light');await f.context.close();checks++;}
    f=await fixture(browser,{broken:true});await f.page.goto('http://knowvia.test/password');await theme(f.page,'light');await appearance(f.page).selectOption('dark');await theme(f.page,'dark');await f.context.close();checks++;
    f=await fixture(browser,{authenticated:true});await f.page.goto('http://knowvia.test/');await f.page.locator('#app-shell').waitFor({state:'visible'});
    await f.page.locator('#profile-trigger').click();await f.page.getByRole('radio',{name:'Dark',exact:true}).click();
    await f.page.locator('#signout-button').click();await f.page.locator('#auth-content').waitFor({state:'visible'});await theme(f.page,'dark');
    assert.equal(await f.page.evaluate(()=>localStorage.getItem('knowvia.appearance')),'dark');assert.equal(await f.page.locator('[data-appearance-control]:visible').count(),0);checks++;await f.context.close();
    for(const width of [390,1280]){
      for(const route of ['/','/admin']){
        f=await fixture(browser,{width,preference:'dark'});await f.page.goto('http://knowvia.test'+route);
        await f.page.locator(route==='/'?'#auth-content':'#login-section').waitFor({state:'visible'});await noOverflow(f.page);
        assert.equal(await f.page.locator('[data-appearance-control]:visible').count(),0);await capture(f.page,'dark-signin-'+(route==='/'?'employee':'admin')+'-'+width);await f.context.close();checks++;
      }
      for(const route of ['/','/admin','/account','/password','/password#purpose=invite&token='+'A'.repeat(43),'/password#purpose=reset&token='+'C'.repeat(43),'/password#purpose=verify&token='+'B'.repeat(43),'/password#purpose=reset&token=invalid']){
        f=await fixture(browser,{width,authenticated:true});await f.page.goto('http://knowvia.test'+route);
        await f.page.evaluate(()=>{localStorage.setItem('knowvia.appearance','dark');window.dispatchEvent(new StorageEvent('storage',{key:'knowvia.appearance',newValue:'dark'}));});await theme(f.page,'dark');
        await f.page.waitForTimeout(150);await noOverflow(f.page);
        const surface=await f.page.locator('body').evaluate(e=>getComputedStyle(e).backgroundColor);assert.equal(surface,'rgb(11, 13, 16)');
        const textColor=await f.page.locator('body').evaluate(e=>getComputedStyle(e).color);assert(contrast(textColor,surface)>=4.5);
        if(route==='/account'){
          const colors=await f.page.locator('#change-form button').evaluate(e=>{const s=getComputedStyle(e);return [s.color,s.backgroundColor];});assert(contrast(...colors)>=4.5,'Primary button text contrast');
          await f.page.locator('#change-form input').first().focus();assert.notEqual(await f.page.locator('#change-form input').first().evaluate(e=>getComputedStyle(e).outlineStyle),'none');
        }
        if(route==='/admin'){
          await f.page.locator('#dashboard').waitFor({state:'visible'});
          if(width<761)await f.page.locator('#menu-toggle').click();
          await f.page.locator('[data-view="employees"]').first().click();
          await capture(f.page,'dark-employees-'+width);
          if(width<761)await f.page.locator('#menu-toggle').click();
          await f.page.locator('[data-view="add"]').first().click();
          await f.page.locator('#onboarding-method-trigger').click();
          assert(await f.page.locator('#onboarding-method-options').isVisible());
          await capture(f.page,'dark-dropdown-'+width);
          await f.page.keyboard.press('Escape');
          if(width<761)await f.page.locator('#menu-toggle').click();
          await f.page.locator('[data-view="knowledge"]').first().click();await capture(f.page,'dark-knowledge-'+width);
          await f.page.locator('#knowledge-skill-trigger').click();assert(await f.page.locator('#knowledge-skill-options').isVisible());await f.page.keyboard.press('Escape');
          if(width<761)await f.page.locator('#menu-toggle').click();
          await f.page.locator('[data-view="employees"]').first().click();await f.page.getByRole('button',{name:'Edit roles',exact:true}).click();
          assert.equal(await f.page.locator('#role-dialog').evaluate(e=>getComputedStyle(e).backgroundColor),'rgb(24, 28, 33)');await capture(f.page,'dark-dialog-'+width);
        }else if(route==='/'){
          await f.page.locator('#app-shell').waitFor({state:'visible'});
          await f.page.evaluate(()=>{
            document.querySelector('#welcome').hidden=true;document.querySelector('.workspace').classList.remove('welcome-state');
            document.querySelector('#messages').innerHTML=renderMessage({role:'user',text:'A fictional workplace question'})+renderMessage({role:'assistant',kind:'KNOWLEDGE',text:'**Grounded guidance**\n\n- First step\n- Second step',sources:[{document:'Demo policy',section:'Supported procedure'}],action:{proposal_id:'demo-only',title:'Mock ticket preview',description:'Requires explicit confirmation.',status:'PROPOSED'}});
          });await f.page.waitForTimeout(250);await capture(f.page,'dark-conversation-'+width);
          assert.equal(await f.page.locator('.source-card').evaluate(e=>getComputedStyle(e).backgroundColor),'rgb(24, 28, 33)');
        }else{
          const label='dark-'+(route.split('#')[0].replaceAll('/','')||'workspace')+(route.includes('#')?'-'+route.split('purpose=')[1].split('&')[0]:'')+(route.endsWith('invalid')?'-invalid':'')+'-'+width;
          await capture(f.page,label);
          if(route.includes('purpose=invite')){
            await f.page.locator('#redeem-form [name=password]').fill('Synthetic-appearance-phrase!');
            await f.page.locator('#redeem-form [name=password_confirmation]').fill('Synthetic-appearance-phrase!');
            await f.page.locator('#redeem-submit').click();await f.page.locator('#success-section').waitFor({state:'visible'});await capture(f.page,'dark-invitation-success-'+width);
          }
        }
        const pale=await f.page.evaluate(()=>Array.from(document.querySelectorAll('body *')).filter(e=>{
          const r=e.getBoundingClientRect(),s=getComputedStyle(e),c=s.backgroundColor.match(/^rgb\((\d+), (\d+), (\d+)\)$/);
          return r.width>100&&r.height>32&&r.bottom>0&&r.top<innerHeight&&s.visibility!=='hidden'&&c&&c.slice(1).every(v=>Number(v)>220);
        }).map(e=>e.tagName+'.'+e.className));
        assert.deepEqual(pale,[],'Unintended light surfaces: '+route+' '+width+' '+JSON.stringify(pale));checks++;await f.context.close();
      }
    }
    // Compare deployed Light baseline without the newly added selector, at matching viewports.
    if(process.env.KNOWVIA_THEME_BASELINE){
      for(const width of [390,1280])for(const [route,authenticated] of [['/',false],['/admin',false],['/account',true],['/',true],['/admin',true]]){
        const images=[];
        for(const baseline of [true,false]){
          f=await fixture(browser,{width,authenticated,baseline});await f.page.goto('http://knowvia.test'+route);
          await f.page.locator(route==='/'?(authenticated?'#app-shell':'#auth-content'):route==='/admin'?(authenticated?'#dashboard':'#login-section'):'#account-section').waitFor({state:'visible'});
          await f.page.addStyleTag({content:'[data-appearance-control]{display:none!important}*{animation:none!important;transition:none!important}'});
          // The in-flow header intentionally changes page height and the gradient
          // sampled through this translucent card. Assert its exact design/geometry
          // instead of requiring identical composited background pixels.
          await f.page.waitForTimeout(200);
          if(route==='/admin'&&!authenticated)images.push(JSON.stringify(await f.page.locator('#login-section').evaluate(e=>{
            const r=e.getBoundingClientRect(),s=getComputedStyle(e);
            return {width:r.width,height:r.height,font:s.font,padding:s.padding,background:s.backgroundColor,border:s.border,borderRadius:s.borderRadius,text:e.innerText,
              fields:Array.from(e.querySelectorAll('input,button')).map(i=>{const b=i.getBoundingClientRect(),c=getComputedStyle(i);return {x:b.x-r.x,y:b.y-r.y,width:b.width,height:b.height,font:c.font,padding:c.padding,border:c.border,borderRadius:c.borderRadius,color:c.color,background:c.backgroundColor};})};
          })));
          else images.push(await f.page.screenshot());
          await capture(f.page,'light-'+(baseline?'baseline-':'new-')+(route.replaceAll('/','')||'signin')+'-'+width);await f.context.close();
        }
        assert(typeof images[0]==='string'?images[0]===images[1]:images[0].equals(images[1]),'Light baseline changed: '+route+' '+width+' '+authenticated);checks++;
      }
    }
    console.log('PASS '+checks+' appearance/browser checks; all network requests intercepted.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
