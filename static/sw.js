/* sw.js — минимальный Service Worker для PWA «Моя Музыка».
 *
 * Кэшируем только «оболочку» приложения. API (/api/) и аудиопотоки
 * (/storage/, /api/stream/) НИКОГДА не попадают в кэш: музыка и данные
 * всегда приходят с сервера.
 */
const CACHE_NAME = 'my-music-shell-v1';

const SHELL = [
    '/',
    '/static/style.css',
    '/static/app.js',
    '/static/manifest.json',
    '/static/icon-192.png',
    '/static/icon-512.png',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then((cache) => cache.addAll(SHELL))
            .then(() => self.skipWaiting()),
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(
                keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
            ))
            .then(() => self.clients.claim()),
    );
});

self.addEventListener('fetch', (event) => {
    const { request } = event;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;

    const path = url.pathname;

    // Динамические данные и аудио — только сеть, без кэша.
    if (path.startsWith('/api/') || path.startsWith('/storage/')
        || path === '/sw.js' || path === '/static/sw.js') {
        return;
    }

    // Навигация: сначала сеть, офлайн — кэшированная главная страница.
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request)
                .then((response) => {
                    if (response.ok) {
                        const copy = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put('/', copy));
                    }
                    return response;
                })
                .catch(() => caches.match('/')),
        );
        return;
    }

    // Статика: кэш с подстраховкой сетью и записью ответа в кэш.
    event.respondWith(
        caches.match(request).then((cached) => cached || fetch(request).then((response) => {
            if (response.ok && response.type === 'basic') {
                const copy = response.clone();
                caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
            }
            return response;
        })),
    );
});
