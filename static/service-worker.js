const CACHE = "ebf-essential-v2";

self.addEventListener("install", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.map(key => caches.delete(key)))));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.map(key => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  // Network only: no background refresh, polling, push or stale API response.
  event.respondWith(fetch(event.request));
});
