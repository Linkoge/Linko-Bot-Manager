'use strict';
// Add User (owner only, never on a restricted instance): form -> plan preview -> Apply, step progress, receipt, Retry.
const onboardingDialog=$('onboarding-dialog');
let onboardingEntries=[],onboardingId=null,onboardingTimer=null,onboardingConfig={},onboardingCommand=null,onboardingLogin=null,onboardingNeedsKey=false;
const onboardingLabels={pending:'Waiting',enrolling:'Waiting for the PC',provisioning:'Provisioning',done:'Done',failed:'Failed'};
function onboardingField(label,name,hint,value=''){const field=element('label',label);const input=element('input');input.name=name;input.value=value;input.autocomplete='off';input.spellcheck=false;if(hint)input.placeholder=hint;field.append(input);return field;}
async function openOnboarding(){
  if(state.restricted)return;
  closeProjectMenu(true);onboardingId=null;onboardingCommand=null;onboardingLogin=null;onboardingNeedsKey=false;
  if(!onboardingDialog.open)onboardingDialog.showModal();
  await loadOnboarding();
}
let onboardingSignature='';
async function loadOnboarding(quiet=false){
  clearTimeout(onboardingTimer);
  const data=await api('/api/onboarding');
  onboardingEntries=data.entries;onboardingConfig=data;
  // A background poll must not redraw (and so wipe) a field the owner is typing into.
  const signature=JSON.stringify([onboardingId,data]);
  if(!quiet||signature!==onboardingSignature)renderOnboarding();
  onboardingSignature=signature;
  const current=onboardingEntries.find(e=>e.id===onboardingId);
  const waiting=current&&(current.busy||current.state==='provisioning'||current.state==='enrolling'&&!current.device_id);
  if(onboardingDialog.open&&(waiting||!onboardingId&&onboardingEntries.some(e=>e.busy||e.state==='provisioning')))onboardingTimer=setTimeout(()=>loadOnboarding(true).catch(()=>{}),2000);
}
function onboardingSteps(entry,parent){
  if(!entry.steps.length&&entry.busy)parent.append(element('p','Checking the host…'));
  const list=element('ul',undefined,'onboarding-steps');
  for(const step of entry.steps){const item=element('li',undefined,'onboarding-step '+step.status);item.append(element('strong',`[${step.status}] ${step.title}`),element('span',' '+step.detail));list.append(item);}
  parent.append(list);
}
function renderOnboarding(){
  const body=$('onboarding-body');body.replaceChildren();$('onboarding-error').textContent='';
  const entry=onboardingEntries.find(e=>e.id===onboardingId);
  if(!entry){renderOnboardingHome(body);return;}
  const head=element('div',undefined,'onboarding-head');
  const back=element('button','← All users');back.type='button';back.onclick=()=>{onboardingId=null;onboardingCommand=null;loadOnboarding();};
  head.append(back,element('strong',`${entry.name} <${entry.email}>`),element('span',onboardingLabels[entry.state],'onboarding-state '+entry.state));body.append(head);
  body.append(element('p',`Windows folder ${entry.windows_folder}`+(entry.hostname?` · https://${entry.hostname}`:entry.url?` · ${entry.url}`:'')));
  body.append(element('p',entry.hostname?'Access: Cloudflare (dedicated tunnel) as requested.':'Access: tailnet only (Tailscale Serve), no Cloudflare.','onboarding-hint'));
  if(entry.error){const error=element('p',entry.error,'onboarding-problem');error.setAttribute('role','alert');body.append(error);}
  body.append(element('h3',entry.state==='done'||entry.state==='provisioning'?'Receipt':entry.state==='failed'?'Last result':'Plan (dry run, nothing has been changed)'));
  onboardingSteps(entry,body);
  if(entry.state==='done'){renderOnboardingDone(entry,body);return;}
  if(entry.state==='provisioning'){body.append(element('p','Provisioning… this page updates by itself.'));return;}
  renderOnboardingEnroll(entry,body);
  const row=element('div',undefined,'dialog-actions');
  const preview=element('button','Re-check plan');preview.type='button';preview.disabled=entry.busy;
  preview.onclick=action(async()=>{await api(`/api/onboarding/${entry.id}/preview`,{method:'POST'});await loadOnboarding();});
  const apply=element('button',entry.state==='failed'?'Retry':'Apply','primary');apply.type='button';apply.disabled=entry.busy||!entry.device_id;
  apply.onclick=action(async()=>{await api(`/api/onboarding/${entry.id}/apply`,{method:'POST'});onboardingCommand=null;await loadOnboarding();});
  row.append(preview,apply);body.append(row);
  if(!entry.device_id)body.append(element('p','Apply unlocks when the PC has reported in (or enter its Syncthing device ID under the command).','onboarding-hint'));
}
function renderOnboardingHome(body){
  const list=element('div',undefined,'onboarding-list');
  for(const entry of onboardingEntries){const open=element('button',`${entry.name} · ${onboardingLabels[entry.state]}`);open.type='button';open.onclick=()=>{onboardingId=entry.id;loadOnboarding();};list.append(open);}
  if(list.children.length)body.append(element('h3','Users'),list);
  if(!onboardingConfig.installed)body.append(element('p','The host helper is not installed yet: see docs/ONBOARDING.md (one-time install).','onboarding-problem'));
  const form=element('form',undefined,'onboarding-form');
  form.append(element('h3','New user'),onboardingField('Name','name','anna'),onboardingField('Email','email','anna@example.com'),onboardingField('Windows folder','windows_folder','C:\\Users\\Anna\\Documents\\Work'));
  const advanced=element('details');advanced.append(element('summary','Advanced'),onboardingField('Cloudflare domain (optional, normally empty: access is over Tailscale only)','domain',''),onboardingField('Memory limit','memory_max','1G'));
  form.append(advanced);
  const submit=element('button','Preview plan','primary');submit.type='submit';form.append(submit);
  form.onsubmit=action(async event=>{event.preventDefault();const values=Object.fromEntries(new FormData(form));
    try{const entry=await api('/api/onboarding',{method:'POST',body:JSON.stringify(values)});onboardingId=entry.id;await loadOnboarding();}
    catch(error){$('onboarding-error').textContent=error.message;}});
  body.append(form);
}
function renderOnboardingPolicy(entry,body){
  // Isolation, layer 2: generated per-user tag + policy snippet. The owner applies it in the Tailscale admin console; this app never does.
  const panel=element('details',undefined,'onboarding-policy');panel.open=entry.state==='done';
  panel.append(element('summary',`Tailscale policy for ${entry.tag}`));
  if(!entry.tailscale_ip)panel.append(element('p','The PC has not reported its Tailscale IP yet, so this instance is not restricted to one client. Re-run Apply once it has reported in.','onboarding-problem'));
  if(!entry.policy){panel.append(element('p','The snippet appears once the plan has allocated this user\'s port (Re-check plan).','onboarding-hint'));body.append(panel);return;}
  panel.append(element('p',`Paste into the tailnet policy file (Access controls). The PC key is tagged ${entry.tag}; it may reach the manager only on its own port and the manager's port. The default allow-all rule must be removed for this to isolate. Nothing here is applied for you.`,'onboarding-hint'));
  const box=element('textarea');box.readOnly=true;box.rows=14;box.value=entry.policy;box.className='onboarding-policy-text';
  const copy=element('button','Copy snippet');copy.type='button';copy.onclick=action(async()=>{box.select();try{await navigator.clipboard.writeText(box.value);}catch{document.execCommand('copy');}});
  panel.append(box,copy);body.append(panel);
}
function renderOnboardingEnroll(entry,body){
  renderOnboardingPolicy(entry,body);
  const panel=element('section',undefined,'onboarding-enroll');panel.append(element('h3','Windows PC'));
  if(entry.device_id){
    panel.append(element('p',`PC reported in: ${entry.pc_hostname||'manual entry'}${entry.tailscale_ip?' ('+entry.tailscale_ip+')':''} · Syncthing ${entry.device_id.slice(0,7)}…`,'onboarding-ok'));
  }else{
    panel.append(element('p','On the new PC, open PowerShell as Administrator and paste the generated line. It installs Tailscale, SSH and Syncthing, then reports back here.'));
  }
  if(onboardingNeedsKey||!onboardingConfig.tailscale_api&&!entry.device_id){
    const key=onboardingField('Tailscale auth key (single use, preauthorized) – no API key is configured','auth_key','tskey-auth-…');key.querySelector('input').id='onboarding-auth-key';key.querySelector('input').type='password';panel.append(key);
  }
  const generate=element('button',onboardingCommand?'Generate a new command':'Generate PC command');generate.type='button';
  generate.onclick=action(async()=>{
    const input=$('onboarding-auth-key');
    try{const result=await api(`/api/onboarding/${entry.id}/enrollment`,{method:'POST',body:JSON.stringify({auth_key:input?input.value||null:null})});
      onboardingCommand=result;onboardingNeedsKey=false;await loadOnboarding();}
    catch(error){if(error.result==='rejected'&&/auth key/i.test(error.message)){onboardingNeedsKey=true;renderOnboarding();}$('onboarding-error').textContent=error.message;}
  });
  panel.append(generate);
  if(onboardingCommand){
    const box=element('textarea');box.readOnly=true;box.rows=4;box.value=onboardingCommand.command;box.id='onboarding-command';
    const copy=element('button','Copy');copy.type='button';copy.onclick=action(async()=>{box.select();try{await navigator.clipboard.writeText(box.value);}catch{document.execCommand('copy');}});
    panel.append(box,copy,element('p',`Single use. Expires ${new Date(onboardingCommand.expires_at*1000).toLocaleTimeString()}. Paste it only on the new PC.`,'onboarding-hint'));
  }
  const manual=onboardingField('Syncthing device ID (only if you do not use the command)','device_id','',entry.device_id);
  const save=element('button','Save ID');save.type='button';save.onclick=action(async()=>{await api(`/api/onboarding/${entry.id}`,{method:'PATCH',body:JSON.stringify({device_id:manual.querySelector('input').value})});await loadOnboarding();});
  const auto=element('label','Apply automatically when the PC reports in');const box=element('input');box.type='checkbox';box.checked=entry.auto_apply;
  box.onchange=action(async()=>{await api(`/api/onboarding/${entry.id}`,{method:'PATCH',body:JSON.stringify({auto_apply:box.checked})});});auto.prepend(box);
  panel.append(auto,element('details'));
  const more=panel.lastChild;more.append(element('summary','Enter a device ID by hand'),manual,save);
  body.append(panel);
}
function renderOnboardingDone(entry,body){
  body.append(element('p',`${entry.name} is ready. Hand over the login below; it is shown once.`));
  renderOnboardingPolicy(entry,body);
  if(onboardingLogin){
    const box=element('pre',`${onboardingLogin.email}\n${onboardingLogin.password}`,'onboarding-login');body.append(box,element('p','Not shown again. The file initial-login.txt on the server is already deleted. The person logs in to their AI provider themselves.','onboarding-hint'));
  }else if(entry.has_login){
    const show=element('button','Show initial login (once)');show.type='button';show.onclick=action(async()=>{onboardingLogin=await api(`/api/onboarding/${entry.id}/login`,{method:'POST'});renderOnboarding();});body.append(show);
  }else body.append(element('p','No initial login to show (an existing login was kept, or it was already shown).','onboarding-hint'));
}
$('onboarding-close').onclick=()=>{onboardingDialog.close();};
onboardingDialog.addEventListener('close',()=>{clearTimeout(onboardingTimer);onboardingCommand=null;onboardingLogin=null;});
