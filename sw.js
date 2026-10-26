const CACHE='ramobet-free-v12';;;;
const ASSETS=['./','./index.html','./manifest.webmanifest','./assets/icon-192.png','./assets/icon-512.png'];
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(CACHE).then(c=>c.addAll(ASSETS)))});
self.addEventListener('activate',e=>e.waitUntil(Promise.all([caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))),self.clients.claim()])));
self.addEventListener('fetch',e=>{
  if(new URL(e.request.url).origin!==location.origin)return;
  const isPage=e.request.mode==='navigate'||new URL(e.request.url).pathname.endsWith('/index.html')||new URL(e.request.url).pathname.endsWith('/');
  const isRecommendations=new URL(e.request.url).pathname.endsWith('/recommendations.json');
  if(isPage||isRecommendations){e.respondWith(fetch(e.request,{cache:'no-store'}).then(resp=>{if(resp.ok){const copy=resp.clone();caches.open(CACHE).then(c=>c.put(e.request,copy));}return resp;}).catch(()=>caches.match(e.request).then(cached=>cached||(isPage?caches.match('./index.html'):new Response(JSON.stringify({status:'offline',recommendations:[],message:'Keine Verbindung zum automatischen Datenfeed.'}),{headers:{'Content-Type':'application/json'}})))));return;}
  e.respondWith(caches.match(e.request).then(cached=>cached||fetch(e.request).then(resp=>{const copy=resp.clone();caches.open(CACHE).then(c=>c.put(e.request,copy));return resp;})));
});
