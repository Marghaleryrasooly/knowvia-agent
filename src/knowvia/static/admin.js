"use strict";
(() => {
  const $ = id => document.getElementById(id);
  // Progressive enhancement: retain the native select as the form authority.
  function enhanceSelect(select) {
    if (!HTMLElement.prototype.showPopover) return;
    const button=document.createElement('button'), menu=document.createElement('div');
    button.type='button';button.className='select-trigger';button.id=select.id+'-trigger';
    button.setAttribute('role','combobox');button.setAttribute('aria-haspopup','listbox');
    button.setAttribute('aria-expanded','false');button.setAttribute('aria-controls',select.id+'-options');
    const label=select.closest('label');
    const title=document.createElement('span');title.id=select.id+'-label';
    title.textContent=label.firstChild.textContent.trim();label.firstChild.replaceWith(title);
    button.setAttribute('aria-labelledby',title.id+' '+button.id);
    menu.id=select.id+'-options';menu.className='select-menu';menu.popover='manual';
    menu.setAttribute('role','listbox');menu.setAttribute('aria-labelledby',title.id);
    let active=select.selectedIndex;
    const options=Array.from(select.options, (option,index)=>{
      const item=document.createElement('div');item.textContent=option.textContent;
      item.id=select.id+'-option-'+index;item.setAttribute('role','option');
      item.addEventListener('pointermove',()=>highlight(index));
      item.addEventListener('mousedown',event=>event.preventDefault());
      item.addEventListener('click',()=>choose(index));menu.append(item);return item;
    });
    function highlight(index){active=index;options.forEach((item,i)=>item.classList.toggle('highlighted',i===index));button.setAttribute('aria-activedescendant',options[index].id);}
    function sync(){button.textContent=select.selectedOptions[0]?.textContent || '';options.forEach((item,i)=>item.setAttribute('aria-selected',String(i===select.selectedIndex)));}
    function close(){menu.hidePopover();button.setAttribute('aria-expanded','false');button.removeAttribute('aria-activedescendant');}
    function position(){
      const rect=button.getBoundingClientRect();menu.style.width=rect.width+'px';menu.style.left=rect.left+'px';
      const height=menu.getBoundingClientRect().height;
      menu.style.top=(rect.bottom+height+8>innerHeight ? Math.max(8,rect.top-height-5) : rect.bottom+5)+'px';
    }
    function open(){
      if(select.disabled || select.closest('[inert]'))return;
      menu.showPopover();position();
      button.setAttribute('aria-expanded','true');highlight(select.selectedIndex);
    }
    function choose(index){select.selectedIndex=index;sync();close();select.dispatchEvent(new Event('change',{bubbles:true}));button.focus();}
    button.addEventListener('click',()=>button.getAttribute('aria-expanded')==='true'?close():open());
    button.addEventListener('keydown',event=>{
      const expanded=button.getAttribute('aria-expanded')==='true';
      if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
        event.preventDefault();if(!expanded)open();
        highlight(event.key==='Home'?0:event.key==='End'?options.length-1:Math.max(0,Math.min(options.length-1,active+(event.key==='ArrowDown'?1:-1))));
      }else if(event.key==='Enter'||event.key===' '){event.preventDefault();expanded?choose(active):open();}
      else if(event.key==='Escape'){event.preventDefault();close();}
      else if(event.key==='Tab')close();
    });
    document.addEventListener('pointerdown',event=>{if(!button.contains(event.target)&&!menu.contains(event.target))close();});
    window.addEventListener('resize',close);document.addEventListener('scroll',()=>{
      if(button.getAttribute('aria-expanded')==='true')position();
    },true);
    select.addEventListener('change',sync);select.form?.addEventListener('reset',()=>queueMicrotask(()=>{sync();close();}));
    label.append(button);document.body.append(menu);sync();select.classList.add('enhanced-select');select.tabIndex=-1;
    // Failure before this point leaves the native control fully usable.
  }
  for(const id of ['onboarding-method','knowledge-skill']){
    const select=$(id);
    try{enhanceSelect(select);}catch(_){
      $(id+'-trigger')?.remove();$(id+'-options')?.remove();
      select.classList.remove('enhanced-select');select.removeAttribute('tabindex');
    }
  }
  const views = {overview:"Overview",employees:"Employees",add:"Add employee",knowledge:"Company knowledge",activity:"Administrative activity"};
  let catalog = [], accounts = [], knowledge = null, epoch = 0, busy = false;
  let savedMessage = "", employeeDirty = false, knowledgeDirty = false, roleEmployee = null;
  let savedKind = 'success';
  let authenticated = false, noticeTimer = null, noticeKind = "";
  let currentView = Object.hasOwn(views, location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  const roleDrafts = new Map();
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  class FeedbackError extends Error {
    constructor(message, code = "VALIDATION") { super(message); this.code = code; }
  }
  function notice(text, kind = "") {
    clearTimeout(noticeTimer); noticeKind=kind;
    $("notice").textContent = text; $("notice").className = kind;
    $("notice").setAttribute("role",kind==="error" ? "alert" : "status");
    $("notice").setAttribute("aria-live",kind==="error" ? "assertive" : "polite");
    $("notification").hidden=!text; $("notification").dataset.kind=kind;
    $("notice-symbol").textContent={success:"✓",warning:"!",error:"!"}[kind] || "i";
    if(kind==="success")noticeTimer=setTimeout(dismissNotice,4500);
    if ($("role-dialog").open) {
      $("notice").setAttribute("role","none");$("notice").setAttribute("aria-live","off");
      $("role-notice").setAttribute("role",kind==="error" ? "alert" : "status");
      $("role-notice").textContent = text; $("role-notice").className = kind;
    }
  }
  function dismissNotice() {
    clearTimeout(noticeTimer); $("notification").hidden=true; $("notice").textContent=""; $("role-notice").textContent="";noticeKind="";
  }
  function readableError(error) {
    return error instanceof FeedbackError ? error.message : "The operation could not be completed. Refresh to check the current state before trying again.";
  }
  function showView(view, focus = false) {
    if (!Object.hasOwn(views, view)) return;
    if(focus && noticeKind!=="error" && noticeKind!=="warning")dismissNotice();
    currentView = view;
    document.querySelectorAll("[data-panel]").forEach(panel => { panel.hidden = panel.dataset.panel !== view; });
    document.querySelectorAll("#admin-navigation [data-view]").forEach(button => {
      if (button.dataset.view === view) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    $("view-title").textContent = views[view];
    history.replaceState(null, "", "#" + view);
    $("admin-navigation").classList.remove("is-open"); $("menu-toggle").setAttribute("aria-expanded", "false");
    if (focus) $("view-title").focus();
  }
  function locked() {
    authenticated=false;
    document.body.classList.add("login-mode");resetLoginPassword();
    epoch += 1; catalog = []; accounts = []; knowledge = null; roleEmployee = null; roleDrafts.clear();
    employeeDirty = false; knowledgeDirty = false;
    if ($("role-dialog").open) $("role-dialog").close();
    $("role-options").replaceChildren();
    $("dashboard").hidden = true; $("login-section").hidden = false; $("logout").hidden = true;
    for (const id of ["accounts","activity","provision-roles","documents","knowledge-roles"]) $(id).replaceChildren();
    $("provision-form").reset(); syncOnboarding(); $("knowledge-form").reset(); $("knowledge-status").textContent = "";
    $("administrator").textContent = ""; $("employee-form-error").textContent = "";
  }
  function mutationMessage(path, body) {
    if (path === "/api/admin/accounts") return "Employee account created successfully.";
    if (path.endsWith('/invite')) return 'Employee account created and invitation email sent.';
    if (path.endsWith('/invitation')) return 'Invitation email resent. Older links are no longer valid.';
    if (path.endsWith("/activation")) return body.active ? "Employee account reactivated. A new sign-in is required." : "Employee account deactivated. Existing sessions were revoked.";
    if (path.endsWith("/roles")) return "Employee roles updated.";
    const messages = {stage:"Document staged successfully. Not yet published.",remove:"Document removal staged, not published.",publish:"Knowledge changes published.",rollback:"Previous knowledge version restored.",discard:"Staged knowledge changes discarded."};
    if (path.startsWith("/api/admin/knowledge/")) return messages[path.split("/").pop()];
    return path === "/api/auth/login" ? "Signed in." : "Change saved.";
  }
  function operationLabel(path) {
    if (path === "/api/admin/accounts") return "Employee creation";
    if (path.endsWith("/activation")) return "Account activation change";
    if (path.endsWith("/roles")) return "Role update";
    const names = {stage:"Document staging",remove:"Document removal staging",publish:"Knowledge publication",rollback:"Knowledge rollback",discard:"Staged-change discard"};
    return path.startsWith("/api/admin/knowledge/") ? names[path.split("/").pop()] : "Request";
  }
  async function api(path, body) {
    let response;
    try {
      response = await fetch(path, {credentials: "same-origin", cache: "no-store", ...(body === undefined ? {} : {
        method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)
      })});
    } catch (_) {
      throw new FeedbackError(body === undefined ? "Connection unavailable. Refresh was not retried." : "The operation could not be confirmed. Check the current state before trying again; the request was not retried.", "UNKNOWN_OUTCOME");
    }
    let data;
    try { data = await response.json(); } catch (_) {
      throw new FeedbackError(body === undefined ? "The server response could not be read." : "The response could not be read; the change may have been saved. Check the current state before resubmitting.", "UNKNOWN_OUTCOME");
    }
    if (!response.ok) {
      if (response.status === 401) {
        const previouslyAuthenticated=authenticated;
        locked();
        throw new FeedbackError(path === "/api/auth/login" ? "Sign-in failed. Check your credentials." : previouslyAuthenticated ? "Your session is unavailable or expired. Please sign in again." : "", path==="/api/auth/login" || previouslyAuthenticated ? "SESSION" : "INITIAL_AUTH");
      }
      if (response.status === 403) {
        if(data?.code==="ORIGIN_REJECTED")throw new FeedbackError("Request origin rejected. Use this site's own administration page; your session has not been signed out.","FORBIDDEN");
        // A shared browser cookie can change identity in another tab. A bounded
        // read verifies authority; never replay the rejected mutation or log out.
        let status=response.status;
        if(path!=="/api/admin/me"){
          try{status=(await fetch("/api/admin/me",{credentials:"same-origin",cache:"no-store"})).status;}catch(_){status=0;}
        }
        if(path==="/api/admin/me" || status===401 || status===403){
          locked();throw new FeedbackError(status===401 ? "Your session is unavailable or expired. Please sign in again." : "The current signed-in account does not have administrator access. Sign in with an administrator account.","ADMIN_REQUIRED");
        }
        throw new FeedbackError("You do not have permission for this operation. Your session has not been signed out.","FORBIDDEN");
      }
      if (response.status === 400 && data?.code === "IDENTITY_CONFLICT")
        throw new FeedbackError("Employee ID or work email already exists. No existing record was changed. Use a new identity; directory-only records cannot be overwritten here.", "IDENTITY_CONFLICT");
      if (response.status === 429) throw new FeedbackError("Too many sign-in attempts. Please wait before trying again.", "RATE_LIMIT");
      const advice=path.endsWith("/activation") ? "Check the account state and last-administrator protection."
        : path.endsWith("/roles") ? "Check trusted roles and last-administrator protection."
        : path==="/api/admin/accounts" ? "Check the employee fields, password requirements and trusted roles."
        : path.endsWith("/stage") ? "Check Markdown format, size, domain, reading roles and current revision."
        : path.endsWith("/rollback") ? "A previous version and no staged changes are required. Refresh to check the revision."
        : path.startsWith("/api/admin/knowledge/") ? "Refresh to check staged changes, the current revision and knowledge integrity."
        : "Check the connection and try again when available.";
      throw new FeedbackError(operationLabel(path)+" could not be completed. "+advice, "REJECTED");
    }
      if (body !== undefined) {
        savedMessage = mutationMessage(path, body);
        if(data.saved && data.mail_sent===false){savedMessage='Employee account saved, but invitation email delivery failed. Do not create it again; use Resend invitation after checking delivery configuration.';savedKind='warning';}
      }
    if(path==="/api/admin/me")authenticated=true;
    return data;
  }
  const selectedRoles = element => Array.from(element.querySelectorAll("input:checked"), input => input.value);
  function roles(selected) {
    const wrap = node("div", undefined, "roles");
    for (const role of catalog) {
      const label = node("label", undefined, "role-choice");
      const input = node("input"); input.type = "checkbox"; input.value = role.name; input.checked = selected.includes(role.name);
      const copy = node("span", role.name);
      copy.append(node("small", role.permissions.length ? role.permissions.join(", ") : "No additional permissions"));
      label.append(input, copy); wrap.append(label);
    }
    return wrap;
  }
  function accountStatus(account) {
    if(account.invitation_pending && account.active && account.account_status==='ACTIVE')return ['Invitation pending','unregistered'];
    if (account.account_status === "NOT_REGISTERED") return account.active ? ["No sign-in account","unregistered"] : ["Inactive directory record","inactive"];
    return account.active && account.account_status === "ACTIVE" ? ["Active","active"] : ["Deactivated","deactivated"];
  }
  function cell(label, child) {
    const td = node("td"); td.dataset.label = label; td.append(child); return td;
  }
  function renderAccounts() {
    const fragment = document.createDocumentFragment();
    for (const account of accounts) {
      const row = node("tr"); row.dataset.employee = account.employee_id;
      const identity = node("div", undefined, "employee-identity");
      identity.append(node("strong", account.first_name + " " + account.last_name), node("small", account.employee_id), node("small", account.work_email, "employee-email"));
      const department = node("div", account.department); department.append(node("small", account.role));
      const [label, style] = accountStatus(account);
      const actions = node("div", undefined, "account-actions");
      const edit = node("button", "Edit roles", "employee-action"); edit.type = "button";
      edit.addEventListener("click", () => openRoleEditor(account.employee_id)); actions.append(edit);
      if (account.account_status !== "NOT_REGISTERED") {
        if(account.invitation_pending){
          const resend=node('button','Resend invitation','employee-action');resend.type='button';
          resend.addEventListener('click',()=>perform(async()=>{await api('/api/admin/accounts/'+encodeURIComponent(account.employee_id)+'/invitation',{});return true;}));actions.append(resend);
        }
        const enable = !account.active || account.account_status !== "ACTIVE";
        const action = node("button", enable ? "Reactivate" : "Deactivate", enable ? "employee-action" : "employee-action danger"); action.type = "button";
        action.addEventListener("click", () => perform(async () => {
          if (!window.confirm((enable ? "Reactivate " : "Deactivate ") + account.employee_id + "?" + (enable ? " A new sign-in will be required." : " Existing sessions will be revoked. History will not be deleted."))) return false;
          await api("/api/admin/accounts/" + encodeURIComponent(account.employee_id) + "/activation", {active:enable});
          return true;
        })); actions.append(action);
      } else actions.append(node("small", "No account to activate"));
      row.append(cell("Employee",identity),cell("Department",department),cell("Status",node("span",label,"badge "+style)),
        cell("Trusted roles",node("div",account.roles.join(", ") || "No assigned roles","role-summary")),cell("Actions",actions));
      fragment.append(row);
    }
    $("accounts").replaceChildren(fragment);
    if (!accounts.length) { const row=node("tr"), td=node("td","No employee records available.");td.colSpan=5;row.append(td);$("accounts").append(row); }
  }
  function openRoleEditor(id) {
    if (busy) return;
    const account = accounts.find(item => item.employee_id === id); if (!account) return;
    roleEmployee = id; $("role-title").textContent = "Edit roles · " + id;
    $("role-notice").textContent = "";
    $("role-options").replaceChildren(roles(roleDrafts.get(id) || account.roles));
    $("role-dialog").showModal();
  }
  function cancelRoleEditor() {
    if (busy) return;
    if (roleDrafts.has(roleEmployee) && !window.confirm("Discard the unsaved role changes?")) return;
    roleDrafts.delete(roleEmployee); roleEmployee = null; $("role-dialog").close();
  }
  function renderActivity(events) {
    const names = {BOOTSTRAP:"Administrator initialized",PROVISION:"Employee account created",ASSIGN_ROLES:"Trusted roles updated",
      ACTIVATE:"Account reactivated",DEACTIVATE:"Account deactivated",KNOWLEDGE_STAGE:"Document staged",KNOWLEDGE_REMOVE:"Document removal staged",
      KNOWLEDGE_PUBLISH:"Knowledge published",KNOWLEDGE_PUBLISH_FAILED:"Knowledge publication failed",KNOWLEDGE_ROLLBACK:"Knowledge rolled back",KNOWLEDGE_DISCARD:"Staged changes discarded"};
    const fragment = document.createDocumentFragment();
    for (const event of events) {
      const row = node("li"); row.append(node("strong",names[event.operation] || "Administrative change"),
        node("p","Actor: "+event.actor+" · Target: "+event.target));
      const date = new Date(event.timestamp);
      const time = node("time",Number.isNaN(date.valueOf()) ? "Time unavailable" : date.toLocaleString()); time.dateTime=event.timestamp; row.append(time);
      let assigned; try { assigned=JSON.parse(event.roles); } catch (_) { assigned=[]; }
      if (Array.isArray(assigned) && assigned.length && assigned.every(role => catalog.some(item=>item.name===role))) {
        const details=node("details"); details.append(node("summary","Recorded role assignment"),node("p",assigned.join(", ")));row.append(details);
      }
      fragment.append(row);
    }
    $("activity").replaceChildren(fragment);
    if (!events.length) $("activity").append(node("li","No administrative activity yet."));
  }
  function knowledgeRoles(force = false) {
    if (!force && $("knowledge-roles").children.length) return;
    const permission = {hr:"knowledge.hr.read",it_support:"knowledge.it.read",finance:"knowledge.finance.read"}[$("knowledge-skill").value];
    const choices = roles([]);
    for (const label of Array.from(choices.children)) {
      const input=label.querySelector("input");
      if (!catalog.find(role=>role.name===input.value).permissions.includes(permission)) label.remove();
    }
    $("knowledge-roles").replaceChildren(choices);
  }
  function renderKnowledge() {
    $("publication-help").textContent=knowledge.integrity_error ? "Publishing is blocked until knowledge integrity is restored." : knowledge.staged ? "Review pending changes before publishing or discarding them." : "Publish and discard are unavailable because no changes are staged.";
    $("rollback-help").textContent=knowledge.staged ? "Publish or discard staged changes before rollback." : knowledge.rollback_available ? "A previous version is available for rollback." : "Rollback is unavailable until a previous published version exists.";
    $("knowledge-status").textContent = knowledge.integrity_error
      ? "Knowledge integrity check failed. Employee retrieval remains blocked; restore the previous working version."
      : "Active version: "+(knowledge.active_version || "reference")+". "+(knowledge.staged ? "Unpublished changes exist." : "No staged changes.");
    const fragment=document.createDocumentFragment();
    for (const doc of knowledge.documents) {
      const row=node("article",undefined,"document-row"), copy=node("div");
      const domains={hr:"Human resources",it_support:"IT support",finance:"Finance"};
      copy.append(node("h3",doc.name),node("p",(domains[doc.skill] || doc.skill)+" · "+(doc.bytes/1024).toFixed(1)+" KiB · Reading roles: "+doc.roles.join(", ")));
      const remove=node("button","Stage removal","danger");remove.type="button";
      remove.addEventListener("click",()=>perform(async()=>{
        if(!window.confirm("Stage removal of "+doc.name+"? Employees keep the current version until publication."))return false;
        await api("/api/admin/knowledge/remove",{name:doc.name,revision:knowledge.revision});return true;
      }));
      row.append(copy,remove);fragment.append(row);
    }
    $("documents").replaceChildren(fragment);
    if (!knowledge.documents.length) $("documents").append(node("p","No documents in this document set.","muted"));
    knowledgeRoles();
  }
  async function load() {
    const current = epoch;
    const me=await api("/api/admin/me"), roleData=await api("/api/admin/roles"), accountData=await api("/api/admin/accounts");
    const auditData=await api("/api/admin/audit"), knowledgeData=await api("/api/admin/knowledge");
    if (current !== epoch) return false;
    catalog=roleData.roles; accounts=accountData.accounts; knowledge=knowledgeData;
    $("administrator").textContent="Signed in as "+me.employee_id;
    renderAccounts();renderActivity(auditData.events);renderKnowledge();
    if (!$("provision-roles").children.length) $("provision-roles").replaceChildren(roles(["employee"]));
    $("summary-active").textContent=String(accounts.filter(a=>accountStatus(a)[1]==="active").length);
    $("summary-unregistered").textContent=String(accounts.filter(a=>a.account_status==="NOT_REGISTERED").length);
    $("summary-documents").textContent=String(knowledge.documents.length);
    $("summary-publication").textContent=knowledge.integrity_error ? "Integrity check failed" : knowledge.staged ? "Staged changes · not yet published" : "Current document set";
    $("summary-activity").textContent=String(auditData.events.length);
    document.body.classList.remove("login-mode");
    $("login-section").hidden=true;$("dashboard").hidden=false;$("logout").hidden=false;showView(currentView);
    controls();return true;
  }
  function controls() {
    document.querySelectorAll("button").forEach(button=>{button.disabled=busy;});
    document.querySelectorAll("form").forEach(form=>{form.inert=busy;});
    $("dashboard").setAttribute("aria-busy",String(busy));
    if(knowledge){
      $("knowledge-publish").disabled=busy || !knowledge.staged || knowledge.integrity_error;
      $("knowledge-discard").disabled=busy || !knowledge.staged;
      $("knowledge-rollback").disabled=busy || !knowledge.rollback_available || knowledge.staged;
    }
  }
  async function perform(operation, reload = true) {
    if (busy) return;
    busy=true;savedMessage="";savedKind='success';controls();
    try {
      const changed=await operation();
      if(changed===false)return;
      const message=savedMessage || "Dashboard refreshed.";
      if(reload){
        try {await load();}
        catch(error){
          notice(message+" Dashboard refresh failed. "+readableError(error)+" The saved operation was not repeated.","warning");
          return;
        }
      }
      notice(message,savedKind);
    } catch(error){notice(readableError(error),"error");}
    finally{busy=false;controls();}
  }
  function resetLoginPassword() {
    $("admin-password").type="password";
    $("admin-password-toggle").textContent="Show";
    $("admin-password-toggle").setAttribute("aria-label","Show password");
    $("admin-password-toggle").setAttribute("aria-pressed","false");
  }
  $("admin-password-toggle").addEventListener("click",()=>{
    const showing=$("admin-password").type==="text";
    $("admin-password").type=showing ? "password" : "text";
    $("admin-password-toggle").textContent=showing ? "Show" : "Hide";
    $("admin-password-toggle").setAttribute("aria-label",showing ? "Show password" : "Hide password");
    $("admin-password-toggle").setAttribute("aria-pressed",String(!showing));
  });
  $("notice-dismiss").addEventListener("click",dismissNotice);
  $("notification").addEventListener("mouseenter",()=>clearTimeout(noticeTimer));
  $("notification").addEventListener("mouseleave",()=>{if(noticeKind==="success")noticeTimer=setTimeout(dismissNotice,4500);});
  $("notification").addEventListener("focusin",()=>clearTimeout(noticeTimer));
  $("notification").addEventListener("focusout",()=>{if(noticeKind==="success")noticeTimer=setTimeout(dismissNotice,4500);});
  document.querySelectorAll("[data-view]").forEach(button=>button.addEventListener("click",()=>showView(button.dataset.view,true)));
  $("menu-toggle").addEventListener("click",()=>{
    const open=$("admin-navigation").classList.toggle("is-open");$("menu-toggle").setAttribute("aria-expanded",String(open));
  });
  $("provision-form").addEventListener("input",()=>{
    employeeDirty=true;$("employee-form-error").textContent="";
    $("provision-form").querySelectorAll("[aria-invalid]").forEach(input=>input.removeAttribute("aria-invalid"));
  });
  $("provision-form").addEventListener("change",()=>{employeeDirty=true;});
  function showSelectedFile(){
    $("knowledge-filename").textContent=$("knowledge-file").files[0]?.name || "No file chosen";
  }
  $("knowledge-file").addEventListener("change",showSelectedFile);
  $("knowledge-form").addEventListener("reset",()=>queueMicrotask(showSelectedFile));
  $("knowledge-form").addEventListener("change",()=>{knowledgeDirty=true;});
  $("employee-clear").addEventListener("click",()=>{
    if(employeeDirty && !window.confirm("Discard the unsaved employee form?"))return;
    $("provision-form").reset();syncOnboarding();$("provision-roles").replaceChildren(roles(["employee"]));employeeDirty=false;$("employee-form-error").textContent="";
  });
  $("role-options").addEventListener("change",()=>roleDrafts.set(roleEmployee,selectedRoles($("role-options"))));
  $("role-cancel").addEventListener("click",cancelRoleEditor);$("role-close").addEventListener("click",cancelRoleEditor);
  $("role-dialog").addEventListener("cancel",event=>{event.preventDefault();cancelRoleEditor();});
  $("role-form").addEventListener("submit",event=>{
    event.preventDefault();
    perform(async()=>{
      const id=roleEmployee, selection=selectedRoles($("role-options"));
      if(!selection.length)throw new FeedbackError("Select at least one trusted role.");
      if(!window.confirm("Save trusted roles for "+id+"? This changes access immediately."))return false;
      await api("/api/admin/accounts/"+encodeURIComponent(id)+"/roles",{roles:selection});
      roleDrafts.delete(id);roleEmployee=null;$("role-dialog").close();return true;
    });
  });
  $("knowledge-skill").addEventListener("change",()=>knowledgeRoles(true));
  $("knowledge-form").addEventListener("submit",event=>{
    event.preventDefault();
    perform(async()=>{
      const file=$("knowledge-file").files[0];
      if(!file || file.size>131072)throw new FeedbackError("Choose a Markdown file no larger than 128 KiB.");
      const selected=selectedRoles($("knowledge-roles"));
      if(!selected.length)throw new FeedbackError("Select explicit document reading roles.");
      const bytes=await file.arrayBuffer();let content;
      try{content=new TextDecoder("utf-8", {fatal: true}).decode(bytes);}catch(_){throw new FeedbackError("Markdown must use valid UTF-8 text.");}
      if(!/^[A-Za-z0-9][A-Za-z0-9_-]{0,90}\.md$/.test(file.name))throw new FeedbackError("Use a simple .md filename with letters, numbers, underscores or hyphens.");
      if(knowledge.documents.some(doc=>doc.name.toLowerCase()===file.name.toLowerCase()) &&
        !window.confirm("Stage a replacement for "+file.name+"? Publication remains a separate step."))return false;
      await api("/api/admin/knowledge/stage",{name: file.name,content,skill:$("knowledge-skill").value,roles:selected,revision: knowledge.revision});
      $("knowledge-form").reset();knowledgeDirty=false;knowledgeRoles(true);return true;
    });
  });
  for(const operation of ["publish","discard","rollback"]){
    $("knowledge-"+operation).addEventListener("click",()=>perform(async()=>{
      const message=operation==="publish" ? "Publish the validated staged corpus?" : operation==="rollback" ? "Restore the previous working corpus?" : "Discard all staged knowledge changes?";
      if(!window.confirm(message))return false;
      if(operation==="publish")notice("Building and validating the staged index. Existing employee requests retain their current snapshot.");
      await api("/api/admin/knowledge/"+operation,{revision: knowledge.revision});return true;
    }));
  }
  $("login-form").addEventListener("submit",event=>{
    event.preventDefault();const form=event.currentTarget;
    perform(async()=>{
      try{await api("/api/auth/login",{work_email:form.elements.work_email.value,password:form.elements.password.value});}
      finally{form.elements.password.value = "";resetLoginPassword();}
      return true;
    });
  });
  $("provision-form").addEventListener("submit",event=>{
    event.preventDefault();const form=event.currentTarget;
    perform(async()=>{
      const body=Object.fromEntries(new FormData(form));body.roles=selectedRoles($("provision-roles"));
      const invitation=body.onboarding==='invite';delete body.onboarding;
      try{
        if(!body.roles.length)throw new FeedbackError("Select at least one trusted role.");
        if(!invitation && (body.password.length<12 || body.password.length>128))throw new FeedbackError("Use a password between 12 and 128 characters.");
        if(!invitation && body.password!==body.password_confirmation){
          form.elements.password_confirmation.setAttribute("aria-invalid","true");
          throw new FeedbackError("Passwords must match.");
        }
        if(["employee_id","work_email","first_name","last_name","department","role"].some(key=>!body[key]?.trim()))throw new FeedbackError("Complete every required employee field.");
        if(invitation){delete body.password;delete body.password_confirmation;}
        await api(invitation?'/api/admin/accounts/invite':"/api/admin/accounts",body);form.reset();syncOnboarding();employeeDirty=false;
        $("provision-roles").replaceChildren(roles(["employee"]));$("employee-form-error").textContent="";
      }catch(error){$("employee-form-error").textContent=readableError(error);throw error;}
      finally{form.elements.password.value = "";form.elements.password_confirmation.value="";}
      return true;
    });
  });
  function syncOnboarding(){
    const invite=$('onboarding-method').value==='invite';
    for(const [name,id] of [['password','initial-password-label'],['password_confirmation','initial-confirmation-label']]){
      const field=$('provision-form').elements[name];field.disabled=invite;field.required=!invite;
      if(invite)field.value='';$(id).hidden=invite;
    }
    $('password-hint').hidden=invite;
  }
  $('onboarding-method').addEventListener('change',syncOnboarding);
  $("refresh").addEventListener("click",()=>perform(async()=>{await load();return true;},false));
  $("logout").addEventListener("click",()=>perform(async()=>{
    if((employeeDirty || roleDrafts.size || knowledgeDirty) && !window.confirm("Sign out and discard unsaved form changes?"))return false;
    await api("/api/auth/logout",{});locked();notice("Signed out.");return false;
  },false));
  window.addEventListener("beforeunload",event=>{
    if(employeeDirty || roleDrafts.size || knowledgeDirty){event.preventDefault();event.returnValue="";}
  });
  load().then(()=>dismissNotice()).catch(error=>{locked();if(error.code==="INITIAL_AUTH")dismissNotice();else notice(readableError(error),"error");});
})();
