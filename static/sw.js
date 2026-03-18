const CACHE_NAME = 'antigravity-v2';
const SHELL_FILES = [
    './',
    'styles.css',
    'app.js',
    'manifest.json',
];

self.addEventListener('install', e => {
    e.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(SHELL_FILES))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', e => {
    e.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', e => {
    // Don't cache API calls
    if (e.request.url.includes('/api/')) {
        return;
    }

    e.respondWith(
        caches.match(e.request).then(cached => {
            // Network-first for HTML, cache-first for static assets
            if (e.request.mode === 'navigate') {
                return fetch(e.request)
                    .then(resp => {
                        const clone = resp.clone();
                        caches.open(CACHE_NAME).then(cache => cache.put(e.request, clone));
                        return resp;
                    })
                    .catch(() => cached);
            }
            return cached || fetch(e.request);
        })
    );
});
