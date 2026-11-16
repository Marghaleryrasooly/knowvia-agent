(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const fragment = new URLSearchParams(location.hash.slice(1));
  let token = fragment.get('token');
  const purpose = fragment.get('purpose');
  history.replaceState(null, '', location.pathname); // Bearer tokens never enter URL queries, storage or logs.
  let busy = false;
  const invalidLink='This link is invalid, expired, or has already been used. Please request a new link.';
  function linkHelp(){
    $('link-help').hidden=false;
    $('link-help-copy').textContent=purpose==='invite'?'Contact your administrator for a new invitation.':purpose==='verify'?'Sign in to Password & Security to request another verification email.':'You can request a new password-reset link using your account email.';
    $('new-reset-link').hidden=purpose!=='reset';
  }
  function feedback(text, error=false) { $('feedback').textContent=text; $('feedback').className=error?'error':''; }
  function success(title, description){
    feedback('');$('success-title').textContent=title;$('success-description').textContent=description;
    $('success-section').hidden=false;$('success-title').focus();
  }
  async function api(operation, body) {
    const response=await fetch('/api/auth/password/'+operation,{credentials:'same-origin',cache:'no-store',
      ...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
    let data;
    try{data=await response.json();}catch(_){throw new Error('The response could not be confirmed. No automatic retry was made.');}
    if(response.status===429)throw new Error('Too many requests. Please wait before trying again. No automatic retry was made.');
    if(!response.ok && operation==='redeem' && response.status===400){
      linkHelp();throw new Error('The request could not be completed. The link may be invalid, expired, or already used. You can request a new link; this response does not identify the exact cause.');
    }
    if(!response.ok) throw new Error(data.error || 'The request could not be completed.');
    return data;
  }
  function bind(id, action) {
    // Keep native validity checks, but provide consistent password-policy copy.
    $(id).noValidate=true;
    $(id).addEventListener('submit',async event=>{
      event.preventDefault(); if(busy || event.currentTarget.dataset.completed==='true')return;
      const form=event.currentTarget;
      const body=Object.fromEntries(new FormData(form));
      if(body.password!==undefined && body.password.length<12){feedback('Your password must contain at least 12 characters.',true);form.elements.password.focus();return;}
      if(body.password!==undefined && body.password.length>128){feedback('Your password must contain no more than 128 characters.',true);form.elements.password.focus();return;}
      if(body.password!==undefined && body.password!==body.password_confirmation){feedback('The passwords do not match. Please try again.',true);form.elements.password_confirmation.focus();return;}
      if(!form.reportValidity())return;
      const submit=form.querySelector('button[type=submit]'),original=submit.textContent;
      busy=true;form.setAttribute('aria-busy','true');form.inert=true;
      submit.textContent=id==='forgot-form'?'Requesting reset link…':id==='redeem-form'?(purpose==='verify'?'Verifying email…':'Saving password…'):id==='change-form'?'Changing password…':'Requesting verification…';
      document.querySelectorAll('button').forEach(b=>{b.disabled=true;});
      try { await action(body); }
      catch(error){feedback(error instanceof TypeError?'Connection failed. The request outcome could not be confirmed. No automatic retry was made.':error.message || 'Request outcome could not be confirmed. No retry was made.',true);}
      finally{form.querySelectorAll('input[type=password]').forEach(input=>{input.value='';});form.inert=false;form.removeAttribute('aria-busy');busy=false;if(submit.textContent!=='Request another link')submit.textContent=original;document.querySelectorAll('button').forEach(b=>{b.disabled=false;});}
    });
  }
  bind('forgot-form',async body=>{const data=await api('forgot',body);feedback(data.message);$('forgot-followup').hidden=false;$('forgot-form').querySelector('button').textContent='Request another link';});
  bind('redeem-form',async body=>{
    if(!token)throw new Error('Open a valid email link.');
    await api('redeem',{token,purpose,...(purpose==='verify'?{}:body)});
    token=null; $('redeem-form').dataset.completed='true';$('redeem-form').hidden=true;
    success(purpose==='verify'?'Recovery email verified':purpose==='invite'?'Your account is ready. You can now sign in.':'Your password has been reset successfully.',
      purpose==='verify'?'Your recovery email is verified. You can return to Knowvia.':purpose==='invite'?'Your password has been saved. Sign in with your account email and the password you just created.':'Your password has been saved and previous sessions have been signed out. Sign in with your new password.');
  });
  bind('change-form',async body=>{await api('change',body);$('account-section').hidden=true;feedback('Password changed. All previous sessions were signed out. Please sign in again.');});
  bind('verify-form',async body=>{await api('verify-email',body);feedback('Verification email sent. Open its link to confirm your recovery address.');});
  if(location.pathname==='/account') {
    $('title').textContent='Password & Security';
    api('status').then(data=>{
      $('account-section').hidden=false;
      $('email-state').textContent=data.email_verified?'Recovery email verified.':'Recovery email is not yet verified.';
      $('email-badge').textContent=data.email_verified?'Verified':'Not verified';
      $('email-badge').className='badge '+(data.email_verified?'verified':'warning');
      $('mail-state').textContent=data.email_delivery_enabled?'Enabled':'Disabled';
      $('mail-state').className='badge '+(data.email_delivery_enabled?'verified':'warning');
      $('mail-notice').hidden=!!data.email_delivery_enabled;
    }).catch(error=>feedback(error.message,true));
  } else if(['invite','reset','verify'].includes(purpose) && (!token || !/^[A-Za-z0-9_-]{43}$/.test(token))) {
    token=null;$('title').textContent='Link unavailable';$('description').textContent='Use a new secure email link to continue.';feedback(invalidLink,true);linkHelp();
  } else if(token && ['invite','reset','verify'].includes(purpose)) {
    $('title').textContent=purpose==='invite'?'Set your password':purpose==='reset'?'Reset your password':'Verify recovery email';
    $('description').textContent=purpose==='invite'?'Welcome to Knowvia. Create a password to finish your invitation.':purpose==='reset'?'Choose a new password to regain access to Knowvia.':'Confirm your recovery email to enable password recovery.';
    $('setup-title').textContent=purpose==='invite'?'Create your password':purpose==='reset'?'Choose a new password':'Confirm your email';
    $('setup-description').textContent=purpose==='invite'?'Your administrator has already assigned your access. This invitation expires after 24 hours.':purpose==='reset'?'This reset link expires after 30 minutes and can be used only once.':'This verification link expires after 30 minutes and can be used only once.';
    $('redeem-form').hidden=false;
    if(purpose==='verify'){
      $('new-passwords').hidden=true;
      $('new-passwords').querySelectorAll('input').forEach(input=>{input.disabled=true;});
      $('redeem-submit').textContent='Verify my email';
    }
  } else {
    $('title').textContent='Forgot password';$('forgot-form').hidden=false;
    $('description').textContent='Enter your Knowvia account email to request a reset link.';
  }
})();
