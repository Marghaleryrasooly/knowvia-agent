(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const fragment = new URLSearchParams(location.hash.slice(1));
  let token = fragment.get('token');
  const purpose = fragment.get('purpose');
  history.replaceState(null, '', location.pathname); // Bearer tokens never enter URL queries, storage or logs.
  let busy = false;
  function feedback(text, error=false) { $('feedback').textContent=text; $('feedback').className=error?'error':''; }
  async function api(operation, body) {
    const response=await fetch('/api/auth/password/'+operation,{credentials:'same-origin',cache:'no-store',
      ...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
    const data=await response.json();
    if(!response.ok) throw new Error(data.error || 'The request could not be completed.');
    return data;
  }
  function bind(id, action) {
    $(id).addEventListener('submit',async event=>{
      event.preventDefault(); if(busy)return;
      const form=event.currentTarget;
      if(!form.reportValidity())return;
      const body=Object.fromEntries(new FormData(form));
      if(body.password!==undefined && body.password!==body.password_confirmation){feedback('Passwords must match.',true);return;}
      busy=true; document.querySelectorAll('button').forEach(b=>{b.disabled=true;});
      try { await action(body); }
      catch(error){feedback(error.message || 'Request outcome could not be confirmed. No retry was made.',true);}
      finally{form.querySelectorAll('input[type=password]').forEach(input=>{input.value='';});busy=false;document.querySelectorAll('button').forEach(b=>{b.disabled=false;});}
    });
  }
  bind('forgot-form',async body=>{const data=await api('forgot',body);feedback(data.message);});
  bind('redeem-form',async body=>{
    if(!token)throw new Error('Open a valid email link.');
    await api('redeem',{token,purpose,...(purpose==='verify'?{}:body)});
    token=null; $('redeem-form').hidden=true;
    feedback(purpose==='verify'?'Recovery email verified.':'Password saved. Please sign in with your new password.');
  });
  bind('change-form',async body=>{await api('change',body);$('account-section').hidden=true;feedback('Password changed. All previous sessions were signed out. Please sign in again.');});
  bind('verify-form',async body=>{await api('verify-email',body);feedback('Verification email sent. Open its link to confirm your recovery address.');});
  if(location.pathname==='/account') {
    $('title').textContent='Password & security';
    api('status').then(data=>{
      $('account-section').hidden=false;
      $('email-state').textContent=data.email_verified?'Recovery email verified.':'Recovery email is not yet verified.';
      if(!data.email_delivery_enabled)feedback('Email delivery is disabled. You can still change your password. Contact the administrator for email recovery.');
    }).catch(error=>feedback(error.message,true));
  } else if(token && ['invite','reset','verify'].includes(purpose)) {
    $('title').textContent=purpose==='invite'?'Set your password':purpose==='reset'?'Reset your password':'Verify recovery email';
    $('redeem-form').hidden=false;
    if(purpose==='verify'){
      $('new-passwords').hidden=true;
      $('new-passwords').querySelectorAll('input').forEach(input=>{input.disabled=true;});
      $('redeem-submit').textContent='Verify my email';
    }
  } else {
    $('title').textContent='Forgot password';$('forgot-form').hidden=false;
    $('description').textContent='Enter your registered, verified recovery email. Reset links expire after 30 minutes.';
  }
})();
