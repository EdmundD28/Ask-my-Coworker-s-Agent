from __future__ import annotations


CHAT_PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Ask My Coworker's Agent — Private Chat</title>
  <style>
    :root{color-scheme:light;--ink:#17211d;--muted:#68736d;--paper:#f4f0e8;--panel:#fffdf8;--line:#ddd7cc;--accent:#176a4a;--accent2:#d9eee4;--danger:#a23a2d}
    *{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,"Microsoft YaHei",sans-serif}
    .shell{min-height:100vh;display:grid;grid-template-columns:320px minmax(0,1fr)}
    aside{border-right:1px solid var(--line);background:#ede8de;padding:26px 18px;display:flex;flex-direction:column;gap:22px}
    .brand{font-size:22px;font-weight:800;letter-spacing:-.02em}.brand small{display:block;font-size:12px;color:var(--muted);font-weight:600;letter-spacing:.08em;text-transform:uppercase}
    .me{padding:14px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.55)}
    .label{font-size:12px;color:var(--muted);margin-bottom:5px}.identity{font-weight:700;overflow-wrap:anywhere}
    .side-title{font-weight:750;margin-bottom:8px}.members,.conversations{display:grid;gap:8px}
    button,select,input,textarea{font:inherit}button{border:0;cursor:pointer}.member,.conversation{width:100%;text-align:left;padding:11px 12px;border-radius:12px;background:transparent;color:var(--ink)}
    .member:hover,.conversation:hover,.conversation.active{background:var(--panel)}.member-name{font-weight:700}.member-id,.conversation-time{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis}
    main{min-width:0;display:grid;grid-template-rows:auto 1fr auto;background:var(--panel)}
    header{padding:22px 28px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:16px;align-items:center}.title{font-size:20px;font-weight:800}.privacy{font-size:12px;color:var(--accent);background:var(--accent2);padding:7px 10px;border-radius:999px}
    #messages{padding:28px;overflow:auto;display:flex;flex-direction:column;gap:14px}.empty{margin:auto;max-width:430px;text-align:center;color:var(--muted)}.empty strong{display:block;color:var(--ink);font-size:22px;margin-bottom:8px}
    .bubble{max-width:min(720px,82%);padding:12px 14px;border-radius:16px;border:1px solid var(--line);background:white;align-self:flex-start}.bubble.mine{align-self:flex-end;background:var(--accent2);border-color:#b8d9c9}.meta{font-size:11px;color:var(--muted);margin-top:7px}.reason{margin-top:7px;padding-top:7px;border-top:1px dashed var(--line);font-size:12px;color:var(--muted)}
    form{border-top:1px solid var(--line);padding:18px 28px 24px;display:grid;gap:10px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:10px}select,input,textarea{width:100%;border:1px solid var(--line);background:white;color:var(--ink);border-radius:10px;padding:10px 12px}textarea{min-height:84px;resize:vertical}.send-row{display:flex;justify-content:space-between;align-items:center;gap:12px}.hint{font-size:12px;color:var(--muted)}.send{background:var(--accent);color:white;font-weight:750;padding:10px 18px;border-radius:10px}.send:disabled{opacity:.45;cursor:not-allowed}.error{color:var(--danger);font-size:13px;min-height:20px}
    @media(max-width:760px){.shell{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--line);padding:16px}.members{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}.conversations{display:none}main{min-height:70vh}.fields{grid-template-columns:1fr}header,#messages,form{padding-left:16px;padding-right:16px}.privacy{display:none}}
  </style>
</head>
<body>
<div class="shell">
  <aside>
    <div class="brand">Ask My Coworker's Agent<small>Private agent chat</small></div>
    <div class="me"><div class="label">Local identity</div><div id="me" class="identity">Loading...</div></div>
    <section><div class="side-title">People you can ask</div><div id="members" class="members"></div></section>
    <section><div class="side-title">My conversations</div><div id="conversations" class="conversations"></div></section>
  </aside>
  <main>
    <header><div><div class="label">Current conversation</div><div id="title" class="title">Choose a member</div></div><div class="privacy">Visible only to both participants</div></header>
    <div id="messages"><div class="empty"><strong>Start with a specific question</strong>You will not see conversations you did not participate in. Private source material is not copied to the coordination space.</div></div>
    <form id="composer">
      <div class="fields"><select id="target" required><option value="">Choose a recipient</option></select><input id="topic" value="general" aria-label="Question topic" placeholder="Topic, for example project.deadline"></div>
      <input id="scope" value="general" aria-label="Project scope" placeholder="Project scope, for example project-blue">
      <textarea id="question" required maxlength="10000" placeholder="Enter the question your agent should ask the other agent"></textarea>
      <div class="send-row"><div><div class="hint">Sending is recorded as an explicit owner action.</div><div id="error" class="error"></div></div><button id="send" class="send" type="submit">Ask</button></div>
    </form>
  </main>
</div>
<script>
(() => {
  const params = new URLSearchParams(location.hash.slice(1));
  if (params.get('token')) sessionStorage.setItem('amca-local-token', params.get('token'));
  const token = sessionStorage.getItem('amca-local-token') || '';
  if (location.hash) history.replaceState(null, '', location.pathname);
  const state = {me:null,members:[],conversation:null};
  const $ = id => document.getElementById(id);
  const api = async (path, options={}) => {
    const response = await fetch(path,{...options,headers:{'Content-Type':'application/json','X-AMCA-Local-Token':token,...(options.headers||{})}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Request failed');
    return data;
  };
  const label = id => state.members.find(x=>x.agent_id===id)?.display_name || id;
  function selectMember(id){$('target').value=id;$('title').textContent=label(id);const pair=[state.me.agent_id,id].sort().join('|');const c=state.conversations?.find(x=>x.participant_ids.slice().sort().join('|')===pair);state.conversation=c?.conversation_id||null;renderConversations();loadMessages();}
  function renderMembers(){const root=$('members');root.replaceChildren();for(const m of state.members){const b=document.createElement('button');b.className='member';b.type='button';const n=document.createElement('div');n.className='member-name';n.textContent=m.display_name;const i=document.createElement('div');i.className='member-id';i.textContent=m.agent_id;b.append(n,i);b.onclick=()=>selectMember(m.agent_id);root.append(b)}if(!state.members.length){root.textContent='No other member has joined this space yet.'}}
  function renderConversations(){const root=$('conversations');root.replaceChildren();for(const c of state.conversations||[]){const b=document.createElement('button');b.type='button';b.className='conversation'+(c.conversation_id===state.conversation?' active':'');const n=document.createElement('div');n.className='member-name';n.textContent=label(c.counterpart_agent_id);const t=document.createElement('div');t.className='conversation-time';t.textContent=new Date(c.updated_at).toLocaleString();b.append(n,t);b.onclick=()=>{state.conversation=c.conversation_id;$('target').value=c.counterpart_agent_id;$('title').textContent=label(c.counterpart_agent_id);renderConversations();loadMessages()};root.append(b)}}
  async function loadMessages(){const root=$('messages');if(!state.conversation){root.innerHTML='<div class="empty"><strong>Start with a specific question</strong>Choose another member, then enter your question.</div>';return}try{const rows=await api('/api/conversations/'+encodeURIComponent(state.conversation)+'/messages');root.replaceChildren();for(const m of rows){const b=document.createElement('div');b.className='bubble'+(m.sender_agent_id===state.me.agent_id?' mine':'');const body=document.createElement('div');body.textContent=m.content || (m.kind==='answer'?'No answer text is available':'');const meta=document.createElement('div');meta.className='meta';meta.textContent=(m.kind==='question'?'Question':'Result')+' · '+m.status+' · '+new Date(m.created_at).toLocaleString();b.append(body,meta);if(m.reason){const r=document.createElement('div');r.className='reason';r.textContent=m.reason;b.append(r)}root.append(b)}root.scrollTop=root.scrollHeight}catch(e){$('error').textContent=e.message}}
  async function refresh(){state.me=await api('/api/me');$('me').textContent=state.me.display_name+' · '+state.me.agent_id;state.members=await api('/api/members');state.conversations=await api('/api/conversations');const selected=$('target').value;$('target').replaceChildren(new Option('Choose a recipient',''));for(const m of state.members)$('target').append(new Option(m.display_name,m.agent_id));if(selected)$('target').value=selected;renderMembers();renderConversations();await loadMessages()}
  $('target').onchange=e=>e.target.value&&selectMember(e.target.value);
  $('composer').onsubmit=async e=>{e.preventDefault();$('error').textContent='';$('send').disabled=true;try{const result=await api('/api/questions',{method:'POST',body:JSON.stringify({target_agent:$('target').value,question:$('question').value,topic:$('topic').value,project_scope:$('scope').value})});state.conversation=result.conversation_id;$('question').value='';await refresh()}catch(err){$('error').textContent=err.message}finally{$('send').disabled=false}};
  refresh().catch(e=>{$('error').textContent=token?'Could not open local chat: '+e.message:"The local access token is missing. Reopen this page from Ask My Coworker's Agent."});
  setInterval(()=>refresh().catch(()=>{}),5000);
})();
</script>
</body></html>'''
