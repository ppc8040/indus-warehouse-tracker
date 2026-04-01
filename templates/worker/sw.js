const CACHE_NAME = 'indus-wh-v2';
const STATIC_ASSETS = [
  '/',
  '/static/css/worker.css',
  '/static/js/app.js',
  '/static/js/offline.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  'https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(names =>
      Promise.all(names.filter(n => n !== CACHE_NAME).map(n => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/admin/')) {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(JSON.stringify({success:false,status:'failed_write_logged',message:'Offline.'}),
          {status:503,headers:{'Content-Type':'application/json'}})
      )
    );
    return;
  }
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(resp => {
        if (resp.status === 200) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return resp;
      }).catch(() => { if (event.request.mode === 'navigate') return caches.match('/'); });
    })
  );
});
