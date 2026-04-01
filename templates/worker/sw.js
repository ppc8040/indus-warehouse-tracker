// Service Worker for Indus Warehouse Tracker PWA
const CACHE_NAME = 'indus-wh-v1';
const STATIC_ASSETS = [
  '/',
  '/static/css/worker.css',
  '/static/js/app.js',
  '/static/js/offline.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

// Install — cache static assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('[SW] Caching static assets');
      return cache.addAll(STATIC_ASSETS);
    })
  );
  self.skipWaiting();
});

// Activate — clean old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) => {
      return Promise.all(
        names.filter(n => n !== CACHE_NAME).map(n => caches.delete(n))
      );
    })
  );
  self.clients.claim();
});

// Fetch — network-first for API, cache-first for static
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // API requests: always go to network, never cache
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/admin/')) {
    event.respondWith(
      fetch(event.request).catch(() => {
        return new Response(
          JSON.stringify({ success: false, status: 'failed_write_logged', message: 'Offline. Request queued.' }),
          { status: 503, headers: { 'Content-Type': 'application/json' } }
        );
      })
    );
    return;
  }

  // Static assets: cache-first, fallback to network
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((resp) => {
        // Cache successful responses for future offline use
        if (resp.status === 200) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return resp;
      }).catch(() => {
        // Ultimate fallback for navigation requests
        if (event.request.mode === 'navigate') {
          return caches.match('/');
        }
      });
    })
  );
});
