'use strict';

const CACHE_PREFIX = 'indus-wh-';
const CACHE_NAME = `${CACHE_PREFIX}{{ deploy_id }}`;

const APP_SHELL = [
  '/',
  '/manifest.json',
  '/static/css/worker.css',
  '/static/js/app.js',
  '/static/js/offline.js',
  '/static/js/loading_review.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        const requests = APP_SHELL.map(
          url => new Request(url, { cache: 'reload' })
        );
        return cache.addAll(requests);
      })
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(names => Promise.all(
        names
          .filter(name =>
            name.startsWith(CACHE_PREFIX) &&
            name !== CACHE_NAME
          )
          .map(name => caches.delete(name))
      ))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);

  try {
    const response = await fetch(request, { cache: 'no-store' });

    if (response && response.ok) {
      await cache.put(request, response.clone());
    }

    return response;
  } catch (error) {
    const cachedResponse =
      await cache.match(request) ||
      await cache.match('/');

    if (cachedResponse) {
      return cachedResponse;
    }

    return new Response(
      '<h1>Offline</h1><p>The portal is unavailable without a network connection.</p>',
      {
        status: 503,
        headers: { 'Content-Type': 'text/html; charset=utf-8' }
      }
    );
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cachedResponse = await cache.match(request);

  if (cachedResponse) {
    return cachedResponse;
  }

  const response = await fetch(request);

  if (response.ok || response.type === 'opaque') {
    await cache.put(request, response.clone());
  }

  return response;
}

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);

  const isServerEndpoint =
    url.origin === self.location.origin &&
    (
      url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/admin/')
    );

  if (isServerEndpoint) {
    event.respondWith(
      fetch(request).catch(() => new Response(
        JSON.stringify({
          success: false,
          status: 'failed_write_logged',
          message: 'Offline.'
        }),
        {
          status: 503,
          headers: { 'Content-Type': 'application/json' }
        }
      ))
    );
    return;
  }

  if (request.method !== 'GET') {
    return;
  }

  // HTML must check Heroku first. Cache is only an offline fallback.
  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request));
    return;
  }

  // Versioned static resources remain fast and available offline.
  event.respondWith(cacheFirst(request));
});
