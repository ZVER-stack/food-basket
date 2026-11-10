function installBridge() {
 if (!window.webpackChunk_N_E || !window.__NEXT_DATA__) return {ready:false};
 const version=window.__NEXT_DATA__?.runtimeConfig?.version;
 if(!version) return {ready:false,phase:'runtime_config'};
 if(version!=='2.86.1') return {ready:false,error:`Изменилась версия web-клиента (${version}); обновите адаптер.`};
 let roots=[];
 for (const el of [document.getElementById('__next'),document,document.documentElement]) {
  if(!el) continue;
  for(const k of Object.keys(el)) if(k.startsWith('__reactContainer$')||k.startsWith('__reactFiber$')) roots.push(el[k]);
 }
 let store=null, seen=new Set();
 while(roots.length && seen.size<40000) {
  let f=roots.pop(); if(!f || seen.has(f)) continue; seen.add(f);
  const s=f.memoizedProps?.store || f.pendingProps?.store;
  if(s?.getState && s?.dispatch) {store=s; break;}
  if(f.child) roots.push(f.child); if(f.sibling) roots.push(f.sibling);
  if(f.stateNode?.current) roots.push(f.stateNode.current);
 }
 if(!store) return {ready:false,phase:'react'};
 if(store.getState().user?.isAuthorized || store.getState().user?.isAuthorizedBySberId)
  return {ready:false,error:'Авторизованная сессия запрещена: используйте гостевую витрину'};
 if(!store.getState().auth?.accessToken) return {ready:false,phase:'guest_session'};
 if (window.__samokatParser?.store === store)
  return {ready:true,version};
 let req;
 window.webpackChunk_N_E.push([[`sno_parser_${Date.now()}`],{},r=>{req=r;}]);
 if (!req) return {ready:false};
 const api=req(50433).u;
 if(!api?.injectEndpoints) return {ready:false,error:'Изменился клиент публичного API'};
 const endpointName='_snoPublicCatalogRead';
 const client=api.endpoints?.[endpointName] ? api : api.injectEndpoints({endpoints:builder=>({
  [endpointName]:builder.query({query:q=>q,keepUnusedDataFor:0})
 })});
 window.__samokatParser={store,async query(q){
  const status=installBridge();
  if(status.error) return {bridgeError:status.error};
  if(!status.ready) return {bridgeState:'not_ready'};
  if(window.__samokatParser.store !== store) return window.__samokatParser.query(q);
  if(store.getState().user?.isAuthorized || store.getState().user?.isAuthorizedBySberId)
   throw Error('Авторизованная сессия запрещена');
  const task=store.dispatch(client.endpoints[endpointName].initiate(q,{forceRefetch:true}));
  const timer=setTimeout(()=>task.abort(),150000);
  try {
   const r=await task;
   return r.error?{error:r.error}:{data:r.data};
  } finally {clearTimeout(timer);task.unsubscribe();}
 }};
 return {ready:true,version:window.__NEXT_DATA__?.runtimeConfig?.version};
}
return installBridge();
