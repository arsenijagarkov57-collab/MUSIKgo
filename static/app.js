/* =========================================================================
   app.js — фронтенд музыкального сервиса на чистом Vanilla JavaScript (ES module).

   Правила безопасности, соблюдённые здесь:
    • названия треков, исполнители и прочие данные приходят с бэкенда и
      записываются ТОЛЬКО через textContent / value / src-свойство;
    • никаких onclick="..." в разметке — все обработчики навешиваются из JS
      через делегирование событий и data-* атрибуты;
    • URL кандидатов не сохраняются в DOM: в атрибуте лежит только индекс,
      а сам объект лежит в state.candidates;
    • innerHTML используется исключительно для статичных SVG-иконок
      (функция icon()), в неё никогда не подставляются пользовательские данные.
   ========================================================================= */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

// ---------------------------------------------------------------------------
// Статичные иконки (только константы!)
// ---------------------------------------------------------------------------
const icon = (name, size = 'w-5 h-5') =>
    `<svg class="${size}" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

// Стильная SVG-заглушка для треков без обложки.
const PLACEHOLDER_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop stop-color="#3a3200"/><stop offset="1" stop-color="#191919"/></linearGradient></defs>
<rect width="300" height="300" fill="url(#g)"/>
<path d="M150 68v124a38 38 0 1 1-20-33V104h76V68h-56Z" fill="#ffcc00" opacity=".9"/>
</svg>`;
const PLACEHOLDER_URL = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(PLACEHOLDER_SVG);

// ---------------------------------------------------------------------------
// Состояние приложения
// ---------------------------------------------------------------------------
const state = {
    tracks: [],          // все скачанные треки (из /api/tracks)
    visible: [],         // список, который сейчас показан — основа очереди
    candidates: [],      // результаты /api/search_candidates
    view: 'home',        // home | likes | search
    query: '',
    current: null,       // текущий трек
    queue: [],
    queueIndex: -1,
    playing: false,
    shuffle: false,
    repeat: 'off',       // off | all | one
    volume: 0.8,
    muted: false,
    loading: new Set(),  // индексы кандидатов, которые уже скачиваются
    likeBusy: new Set(),
    searchTimer: null,
    searchSeq: 0,
    searchController: null,
    remoteLoading: false,
    remoteError: '',
    playlists: [],       // список плейлистов (/api/playlists)
    activePlaylistId: null,
    playlistTracks: [],  // треки открытого плейлиста
    lyrics: { trackId: null, lines: [], nodes: [], activeIndex: -1, text: '' },
    wave: {
        active: false,
        loading: false,
        exclude: [],       // уже сыгранные треки волны
        nextTrack: null,   // предзагруженный следующий трек
        preloadAudio: null,
        prefetchFor: null, // id трека, для которого уже был запрос предзагрузки
    },
};

let playlistPicker = null;

// Установка приложения: PWA в 1 клик и «умная» ссылка на APK.
const APK_CHECK_URL = '/download/app';
const APK_FALLBACK_PATH = '/static/MyMusic.apk';
let deferredPrompt = null;   // событие beforeinstallprompt (вызываем .prompt())
let apkReady = false;        // результат HEAD /download/app
let installLastFocus = null;

const audio = new Audio();
audio.preload = 'metadata';
// На iOS элемент должен быть в DOM и иметь playsinline: иначе Safari
// открывает полноэкранный плеер и может остановить звук при блокировке экрана.
audio.playsInline = true;
audio.setAttribute('playsinline', '');
audio.setAttribute('webkit-playsinline', '');
audio.hidden = true;
document.body.appendChild(audio);

// ---------------------------------------------------------------------------
// Утилиты
// ---------------------------------------------------------------------------
function el(tag, className = '', options = {}) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (options.text !== undefined) node.textContent = String(options.text);
    for (const [key, value] of Object.entries(options.attrs || {})) {
        node.setAttribute(key, String(value));
    }
    return node;
}

function formatTime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = String(total % 60).padStart(2, '0');
    return hours
        ? `${hours}:${String(minutes).padStart(2, '0')}:${secs}`
        : `${String(minutes).padStart(2, '0')}:${secs}`;
}

function plural(count, one, few, many) {
    const n = Math.abs(count) % 100;
    const last = n % 10;
    if (n > 10 && n < 20) return many;
    if (last > 1 && last < 5) return few;
    if (last === 1) return one;
    return many;
}

const trackCount = (n) => `${n} ${plural(n, 'трек', 'трека', 'треков')}`;
const isLiked = (track) => Boolean(track && track.is_liked);

function matchesQuery(track, query) {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return false;
    return (String(track.title || '').toLocaleLowerCase().includes(needle)
        || String(track.artist || '').toLocaleLowerCase().includes(needle));
}

function setImage(img, url) {
    img.dataset.fallback = url ? '0' : '1';
    img.src = url || PLACEHOLDER_URL;
}

function makeCover(url, className) {
    const img = el('img', className, { attrs: { alt: '', loading: 'lazy', decoding: 'async' } });
    setImage(img, url);
    img.addEventListener('error', () => {
        if (img.dataset.fallback !== '1') {
            img.dataset.fallback = '1';
            img.src = PLACEHOLDER_URL;
        }
    });
    return img;
}

function setRangeFill(input) {
    const min = Number(input.min) || 0;
    const max = Number(input.max) || 0;
    const value = Number(input.value) || 0;
    const percent = max > min ? ((value - min) / (max - min)) * 100 : 0;
    input.style.setProperty('--value', `${percent}%`);
}

function setShown(node, shown) {
    node.classList.toggle('hidden', !shown);
}

function setEmptyShown(node, shown) {
    node.classList.toggle('hidden', !shown);
    node.classList.toggle('flex', shown);
}

// ---------------------------------------------------------------------------
// HTTP-клиент
// ---------------------------------------------------------------------------
async function request(path, options = {}) {
    const response = await fetch(path, {
        ...options,
        headers: options.body
            ? { 'Content-Type': 'application/json', ...(options.headers || {}) }
            : options.headers,
    });
    if (!response.ok) {
        let detail = '';
        try {
            const payload = await response.json();
            detail = typeof payload?.detail === 'string'
                ? payload.detail
                : payload?.detail ? JSON.stringify(payload.detail) : '';
        } catch (_) { /* пустое тело ответа */ }
        throw new Error(detail || `Ошибка сервера (${response.status})`);
    }
    return response.json();
}

function toast(message, kind = 'info') {
    const box = $('#toasts');
    const item = el('div', `toast ${kind === 'error' ? 'error' : ''}`, { text: message });
    box.append(item);
    requestAnimationFrame(() => item.classList.add('show'));
    window.setTimeout(() => {
        item.classList.remove('show');
        window.setTimeout(() => item.remove(), 300);
    }, 4200);
}

// ---------------------------------------------------------------------------
// Рендер списков
// ---------------------------------------------------------------------------
function buildLikeButton(track) {
    const liked = isLiked(track);
    const button = el('button',
        `like-btn shrink-0 grid place-items-center rounded-full transition-colors ${liked ? 'liked' : 'text-white/30 hover:text-white'} w-9 h-9`,
        { attrs: { type: 'button', 'aria-label': liked ? 'Убрать из коллекции' : 'Добавить в коллекцию' } });
    button.dataset.action = 'like';
    button.dataset.id = String(track.id);
    button.innerHTML = icon(liked ? 'heart-fill' : 'heart', 'w-5 h-5');
    return button;
}

function buildRowAction(action, id, iconName, label, extraClass = '') {
    const button = el('button', `row-action-btn ${extraClass}`, {
        attrs: { type: 'button', 'aria-label': label, title: label },
    });
    button.dataset.action = action;
    button.dataset.id = String(id);
    button.innerHTML = icon(iconName, 'w-4 h-4');
    return button;
}

function buildTrackRow(track, index) {
    const isCurrent = state.current?.id === track.id;
    const row = el('div',
        `track-row group flex items-center gap-3 rounded-xl px-2 py-2 md:px-3 ${isCurrent ? 'playing' : ''}`,
        { attrs: { role: 'button', tabindex: '0', 'aria-label': `Играть: ${track.title || ''}` } });
    row.dataset.action = 'track';
    row.dataset.id = String(track.id);
    row.dataset.index = String(index);

    const coverBox = el('div', 'relative w-12 h-12 md:w-14 md:h-14 shrink-0 rounded-lg overflow-hidden bg-[#242424]');
    coverBox.append(makeCover(track.cover_url, 'w-full h-full object-cover'));

    const overlay = el('button', 'row-play-overlay absolute inset-0 grid place-items-center bg-black/45 text-white',
        { attrs: { type: 'button', 'aria-label': isCurrent && state.playing ? 'Пауза' : 'Играть' } });
    overlay.dataset.action = 'row-play';
    overlay.dataset.id = String(track.id);
    overlay.dataset.index = String(index);
    overlay.innerHTML = icon(isCurrent && state.playing ? 'pause' : 'play', 'w-5 h-5');
    coverBox.append(overlay);

    if (isCurrent && state.playing) {
        const eq = el('div', 'eq absolute bottom-0 right-0');
        eq.innerHTML = '<i></i><i></i><i></i>';
        coverBox.append(eq);
    }

    const info = el('div', 'min-w-0 flex-1');
    info.append(
        el('p', 'track-title truncate text-sm font-semibold', { text: track.title || 'Без названия' }),
        el('p', 'truncate text-xs text-white/40 mt-0.5', { text: track.artist || 'Неизвестный исполнитель' }),
    );

    const duration = el('span', 'hidden sm:block shrink-0 w-12 text-right text-xs tabular-nums text-white/35',
        { text: formatTime(track.duration) });

    row.append(
        coverBox,
        info,
        duration,
        buildRowAction('add-to-playlist', track.id, 'list-plus', 'Добавить в плейлист'),
        buildRowAction('delete-track', track.id, 'trash', 'Удалить трек', 'danger'),
        buildLikeButton(track),
    );
    return row;
}

function renderTracks(container, tracks, showEmptyMessage = true) {
    container.replaceChildren();
    if (!tracks.length) {
        if (showEmptyMessage) {
            container.append(el('p', 'text-sm text-white/30 px-3 py-4', { text: 'Ничего не найдено' }));
        }
        return;
    }
    const fragment = document.createDocumentFragment();
    tracks.forEach((track, index) => fragment.append(buildTrackRow(track, index)));
    container.append(fragment);
}

function skeletonRow() {
    const row = el('div', 'flex items-center gap-3 px-2 py-2');
    row.append(el('div', 'skeleton w-12 h-12 shrink-0'));
    const lines = el('div', 'flex-1 space-y-2');
    lines.append(el('div', 'skeleton h-3 w-2/5'), el('div', 'skeleton h-2.5 w-1/4'));
    row.append(lines);
    return row;
}

function buildCandidateCard(candidate, index) {
    const loading = state.loading.has(index);
    const isYouTube = candidate.source === 'youtube';
    const card = el('div',
        `candidate group flex items-center gap-3 rounded-xl px-2 py-2.5 md:px-3 cursor-pointer transition-colors hover:bg-white/5 ${loading ? 'candidate-loading' : ''}`);
    card.dataset.action = 'candidate';
    card.dataset.index = String(index);
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');

    card.append(makeCover(candidate.thumbnail, 'w-12 h-12 shrink-0 rounded-lg object-cover bg-[#242424]'));

    const info = el('div', 'min-w-0 flex-1');
    info.append(el('p', 'truncate text-sm font-semibold', { text: candidate.title || 'Без названия' }));

    const meta = el('div', 'flex items-center gap-2 mt-1 min-w-0');
    meta.append(el('span',
        `shrink-0 rounded px-1.5 py-0.5 text-[10px] font-extrabold leading-none ${
            isYouTube ? 'bg-red-500/15 text-red-300' : 'bg-orange-400/15 text-orange-300'}`,
        { text: isYouTube ? 'YT' : 'SC' }));
    meta.append(el('span', 'truncate text-xs text-white/40', { text: candidate.artist || 'Неизвестный исполнитель' }));
    if (candidate.duration) {
        meta.append(el('span', 'shrink-0 text-xs tabular-nums text-white/30', { text: formatTime(candidate.duration) }));
    }
    info.append(meta);
    card.append(info);

    const action = el('div', 'shrink-0 w-8 grid place-items-center');
    if (loading) {
        action.innerHTML = '<span class="spinner"></span>';
    } else {
        action.innerHTML = icon('download', 'w-5 h-5 text-white/30 group-hover:text-accent transition-colors');
    }
    card.append(action);
    return card;
}

function renderCandidates() {
    const box = $('#searchRemote');
    box.replaceChildren();

    if (state.remoteLoading) {
        for (let i = 0; i < 3; i += 1) box.append(skeletonRow());
        return;
    }
    if (!state.candidates.length) {
        box.append(el('p', 'text-sm text-white/30 px-2 py-4', {
            text: state.remoteError ? 'Нет результатов' : 'По вашему запросу в сети ничего не найдено',
        }));
        return;
    }

    const fragment = document.createDocumentFragment();
    state.candidates.forEach((candidate, index) => fragment.append(buildCandidateCard(candidate, index)));
    box.append(fragment);
}

function renderSearch() {
    const query = state.query.trim();
    setShown($('#searchIdle'), !query);
    setShown($('#searchResults'), Boolean(query));

    const errorNode = $('#remoteError');
    errorNode.textContent = state.remoteError;
    setShown(errorNode, Boolean(state.remoteError));

    if (!query) return;

    const local = state.tracks.filter((track) => matchesQuery(track, query));
    state.visible = local;
    $('#localCount').textContent = String(local.length);
    setShown($('#searchLocalSection'), local.length > 0);
    renderTracks($('#searchLocal'), local);
    renderCandidates();
}

function renderAll() {
    const liked = state.tracks.filter(isLiked);

    renderTracks($('#homeTracks'), state.tracks, false);
    renderTracks($('#likesTracks'), liked, false);
    setEmptyShown($('#homeEmpty'), state.tracks.length === 0);
    setEmptyShown($('#likesEmpty'), liked.length === 0);

    $('#homeCount').textContent = trackCount(state.tracks.length);
    $('#likesCount').textContent = trackCount(liked.length);
    $('#heroPlay').disabled = state.tracks.length === 0;

    if (state.view === 'search') {
        renderSearch();
    } else if (state.view === 'playlist') {
        const playlist = state.playlists.find((item) => item.id === state.activePlaylistId);
        $('#playlistTitle').textContent = playlist?.name || 'Плейлист';
        $('#playlistCount').textContent = trackCount(state.playlistTracks.length);
        renderTracks($('#playlistTracks'), state.playlistTracks, false);
        setEmptyShown($('#playlistEmpty'), state.playlistTracks.length === 0);
        state.visible = state.playlistTracks;
    } else {
        state.visible = state.view === 'likes' ? liked : state.tracks;
    }

    renderPlaylists();
}

// ---------------------------------------------------------------------------
// Плейлисты
// ---------------------------------------------------------------------------
function renderPlaylists() {
    for (const container of [$('#playlistNav'), $('#mobilePlaylistNav')]) {
        if (!container) continue;
        container.replaceChildren();
        if (!state.playlists.length) {
            container.append(el('p', 'text-xs text-white/25 px-2 py-1.5', {
                text: 'Пока нет плейлистов',
            }));
            continue;
        }
        const fragment = document.createDocumentFragment();
        for (const playlist of state.playlists) {
            const active = state.view === 'playlist' && playlist.id === state.activePlaylistId;
            const item = el('div', `playlist-item ${active ? 'active' : ''}`, {
                attrs: { role: 'button', tabindex: '0', 'aria-label': `Открыть плейлист: ${playlist.name}` },
            });
            item.dataset.action = 'open-playlist';
            item.dataset.id = String(playlist.id);
            item.append(
                el('span', 'truncate flex-1', { text: playlist.name }),
                el('span', 'shrink-0 text-[10px] tabular-nums text-white/30',
                    { text: String(playlist.track_count) }),
            );

            const remove = el('button', 'playlist-remove shrink-0 w-6 h-6 grid place-items-center rounded-md text-white/30',
                { attrs: { type: 'button', 'aria-label': `Удалить плейлист: ${playlist.name}`, title: 'Удалить плейлист' } });
            remove.dataset.action = 'delete-playlist';
            remove.dataset.id = String(playlist.id);
            remove.innerHTML = icon('close', 'w-3.5 h-3.5');
            item.append(remove);
            fragment.append(item);
        }
        container.append(fragment);
    }
}

async function loadPlaylists() {
    try {
        const playlists = await request('/api/playlists');
        state.playlists = Array.isArray(playlists) ? playlists : [];
    } catch (error) {
        toast(`Не удалось загрузить плейлисты: ${error.message}`, 'error');
    }
}

async function reloadPlaylistTracks() {
    if (state.activePlaylistId === null) return;
    try {
        const tracks = await request(`/api/playlists/${state.activePlaylistId}/tracks`);
        state.playlistTracks = Array.isArray(tracks) ? tracks : [];
        const playlist = state.playlists.find((item) => item.id === state.activePlaylistId);
        if (playlist) playlist.track_count = state.playlistTracks.length;
    } catch (error) {
        toast(`Не удалось загрузить треки плейлиста: ${error.message}`, 'error');
    }
}

async function openPlaylist(id) {
    const playlistId = Number(id);
    state.activePlaylistId = playlistId;
    await reloadPlaylistTracks();
    closeMobilePlaylists();
    setView('playlist');
}

async function createPlaylist(trackId = null) {
    const name = window.prompt('Название нового плейлиста:', 'Новый плейлист');
    if (name === null) return;
    const trimmed = name.trim();
    if (!trimmed) {
        toast('Название плейлиста не может быть пустым', 'error');
        return;
    }
    try {
        const playlist = await request('/api/playlists', {
            method: 'POST', body: JSON.stringify({ name: trimmed }),
        });
        playlist.track_count = 0;
        state.playlists.unshift(playlist);
        closePlaylistPicker();

        if (trackId) {
            await request(`/api/playlists/${playlist.id}/tracks`, {
                method: 'POST', body: JSON.stringify({ track_id: Number(trackId) }),
            });
            playlist.track_count = 1;
            toast(`Трек добавлен в плейлист «${playlist.name}»`);
        } else {
            toast(`Плейлист «${playlist.name}» создан`);
        }
        renderAll();
    } catch (error) {
        toast(`Не удалось создать плейлист: ${error.message}`, 'error');
    }
}

async function deletePlaylist(id) {
    const playlist = state.playlists.find((item) => item.id === Number(id));
    if (!playlist || !window.confirm(`Удалить плейлист «${playlist.name}»?`)) return;
    try {
        await request(`/api/playlists/${playlist.id}`, { method: 'DELETE' });
        state.playlists = state.playlists.filter((item) => item.id !== playlist.id);
        if (state.activePlaylistId === playlist.id) {
            state.activePlaylistId = null;
            state.playlistTracks = [];
            setView('home');
        } else {
            renderAll();
        }
        toast('Плейлист удалён');
    } catch (error) {
        toast(`Не удалось удалить плейлист: ${error.message}`, 'error');
    }
}

async function addTrackToPlaylist(playlistId, trackId) {
    closePlaylistPicker();
    try {
        await request(`/api/playlists/${playlistId}/tracks`, {
            method: 'POST', body: JSON.stringify({ track_id: Number(trackId) }),
        });
        const playlist = state.playlists.find((item) => item.id === Number(playlistId));
        if (playlist) playlist.track_count = Number(playlist.track_count) + 1;
        if (state.activePlaylistId === Number(playlistId)) await reloadPlaylistTracks();
        renderAll();
        toast(`Трек добавлен в плейлист «${playlist?.name || ''}»`);
    } catch (error) {
        toast(`Не удалось добавить трек: ${error.message}`, 'error');
    }
}

function closePlaylistPicker() {
    if (playlistPicker) {
        playlistPicker.remove();
        playlistPicker = null;
    }
}

function openPlaylistPicker(trackId, anchor) {
    closePlaylistPicker();
    const menu = el('div', 'playlist-picker');
    menu.append(el('p', 'px-2 pb-1.5 text-[10px] font-bold uppercase tracking-wider text-white/35',
        { text: 'Добавить в плейлист' }));

    if (!state.playlists.length) {
        menu.append(el('p', 'px-2 py-2 text-xs text-white/35', { text: 'Плейлистов пока нет' }));
    }
    for (const playlist of state.playlists) {
        const button = el('button', '', { text: playlist.name });
        button.dataset.action = 'picker-add';
        button.dataset.id = String(trackId);
        button.dataset.playlistId = String(playlist.id);
        menu.append(button);
    }

    const create = el('button', 'border-t border-white/10 mt-1 pt-2 text-accent', { text: '+ Новый плейлист' });
    create.dataset.action = 'picker-create';
    create.dataset.id = String(trackId);
    menu.append(create);

    document.body.append(menu);
    playlistPicker = menu;

    const rect = anchor.getBoundingClientRect();
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.left = `${Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(rect.top - height - 8, window.innerHeight - height - 8))}px`;
}

async function removeTrack(trackId) {
    const track = findTrack(Number(trackId), state.tracks);
    if (!track || !window.confirm(`Удалить «${track.title}» из библиотеки и с диска?`)) return;
    try {
        await request(`/api/tracks/${track.id}`, { method: 'DELETE' });
        state.tracks = state.tracks.filter((item) => item.id !== track.id);
        state.playlistTracks = state.playlistTracks.filter((item) => item.id !== track.id);
        for (const playlist of state.playlists) {
            playlist.track_count = Math.max(0, Number(playlist.track_count) - 1);
        }
        if (state.current?.id === track.id) {
            stopWave();
            closeLyrics();
            audio.pause();
            audio.removeAttribute('src');
            audio.load();
            state.current = null;
            state.queue = [];
            state.queueIndex = -1;
            state.playing = false;
            updatePlayer();
            updateMediaSession();
        }
        renderAll();
        toast('Трек удалён из библиотеки');
    } catch (error) {
        toast(`Не удалось удалить трек: ${error.message}`, 'error');
    }
}

function openMobilePlaylists() {
    setShown($('#mobilePlaylistOverlay'), true);
    setShown($('#mobilePlaylistDrawer'), true);
}

function closeMobilePlaylists() {
    setShown($('#mobilePlaylistOverlay'), false);
    setShown($('#mobilePlaylistDrawer'), false);
}

// ---------------------------------------------------------------------------
// Навигация и поиск
// ---------------------------------------------------------------------------
function setView(view) {
    state.view = view;
    $$('[data-view-panel]').forEach((panel) => setShown(panel, panel.dataset.viewPanel === view));
    $$('[data-action="view"]').forEach((button) => {
        const active = button.dataset.view === view;
        button.classList.toggle('active', active);
        if (active) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
    });
    renderAll();
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function focusSearch() {
    const input = $('#searchInput');
    input.focus();
    input.select();
}

function clearSearch() {
    if (state.searchTimer) window.clearTimeout(state.searchTimer);
    state.searchTimer = null;
    state.searchSeq += 1;
    state.searchController?.abort();
    state.searchController = null;
    state.query = '';
    state.candidates = [];
    state.remoteLoading = false;
    state.remoteError = '';
    state.loading.clear();
    $('#searchInput').value = '';
    setShown($('#clearSearch'), false);
    renderAll();
}

async function runSearch() {
    const query = state.query.trim();
    if (!query) {
        clearSearch();
        return;
    }

    renderSearch(); // локальная библиотека фильтруется мгновенно

    const seq = ++state.searchSeq;
    state.searchController?.abort();
    const controller = new AbortController();
    state.searchController = controller;
    state.remoteLoading = true;
    state.remoteError = '';
    state.candidates = [];
    state.loading.clear();
    renderCandidates();

    try {
        const data = await request(`/api/search_candidates?q=${encodeURIComponent(query)}`, {
            signal: controller.signal,
        });
        if (seq !== state.searchSeq) return;
        state.candidates = Array.isArray(data) ? data : [];
    } catch (error) {
        if (error.name === 'AbortError' || seq !== state.searchSeq) return;
        state.candidates = [];
        state.remoteError = `Не удалось выполнить поиск в сети: ${error.message}`;
        toast(state.remoteError, 'error');
    } finally {
        if (seq === state.searchSeq) {
            state.remoteLoading = false;
            renderCandidates();
            const errorNode = $('#remoteError');
            errorNode.textContent = state.remoteError;
            setShown(errorNode, Boolean(state.remoteError));
        }
    }
}

function scheduleSearch() {
    if (state.searchTimer) window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(() => runSearch(), 500);
}

async function downloadCandidate(index) {
    const candidate = state.candidates[index];
    if (!candidate || state.loading.has(index)) return; // защита от повторных кликов

    state.loading.add(index);
    renderCandidates();

    try {
        const track = await request('/api/download', {
            method: 'POST',
            body: JSON.stringify({ url: candidate.url, source: candidate.source }),
        });

        // Добавляем в начало локального списка, проверяя дубликат по id.
        const existing = state.tracks.findIndex((item) => item.id === track.id);
        if (existing >= 0) state.tracks.splice(existing, 1);
        state.tracks.unshift(track);

        // Очищаем поиск: трек теперь в библиотеке и готов к воспроизведению.
        if (state.searchTimer) window.clearTimeout(state.searchTimer);
        state.searchSeq += 1;
        state.searchController?.abort();
        state.searchController = null;
        state.query = '';
        state.candidates = [];
        state.remoteLoading = false;
        state.remoteError = '';
        state.loading.clear();
        $('#searchInput').value = '';
        setShown($('#clearSearch'), false);

        const queue = [track, ...state.tracks.filter((item) => item.id !== track.id)];
        setView('home');
        playTrack(track, queue, 0);
        toast(`«${track.title || 'Трек'}» добавлен в библиотеку`);
    } catch (error) {
        state.loading.delete(index);
        renderCandidates();
        toast(`Не удалось скачать трек: ${error.message}`, 'error');
    }
}

// ---------------------------------------------------------------------------
// Воспроизведение
// ---------------------------------------------------------------------------
function findTrack(id, list) {
    return (list || []).find((track) => track.id === id)
        || state.tracks.find((track) => track.id === id)
        || null;
}

function playTrackById(id, list, preferredIndex = -1) {
    const track = findTrack(id, list);
    if (!track) return;
    const source = list && list.length ? list : state.tracks;
    const index = preferredIndex >= 0 && source[preferredIndex]?.id === track.id
        ? preferredIndex
        : source.findIndex((item) => item.id === track.id);
    playTrack(track, source, index);
}

function playTrack(track, queue, index = -1) {
    if (!track) return;
    closeLyrics(); // текст предыдущего трека больше не актуален
    state.wave.prefetchFor = null;
    state.current = track;
    state.queue = queue && queue.length ? [...queue] : [track];
    state.queueIndex = index >= 0 ? index : Math.max(0, state.queue.findIndex((item) => item.id === track.id));

    audio.src = `/api/stream/${track.id}`;
    audio.load();

    const promise = audio.play();
    if (promise && typeof promise.catch === 'function') {
        promise.catch(() => {
            // Браузер может заблокировать автозапуск — пользователь нажмёт Play вручную.
            state.playing = false;
            updatePlayer();
            renderAll();
        });
    }
    updatePlayer();
    updateMediaSession();
    renderAll();
}

function togglePlay() {
    if (!state.current) {
        const first = state.visible[0] || state.tracks[0];
        if (!first) return;
        playTrack(first, state.visible.length ? state.visible : state.tracks, 0);
        return;
    }
    if (audio.paused) {
        const promise = audio.play();
        if (promise && typeof promise.catch === 'function') {
            promise.catch(() => toast('Браузер заблокировал воспроизведение — нажмите Play', 'error'));
        }
    } else {
        audio.pause();
    }
}

function nextTrack(auto = false) {
    if (!state.queue.length && !state.wave.active) return;

    // В режиме волны следующий трек уже может быть предзагружен — ставим
    // его мгновенно, без запроса к серверу в момент окончания песни.
    if (state.wave.active) {
        if (auto && state.repeat === 'one') {
            audio.currentTime = 0;
            const promise = audio.play();
            if (promise && typeof promise.catch === 'function') promise.catch(() => {});
            return;
        }
        const ready = state.wave.nextTrack;
        if (ready) {
            rememberCurrentForWave();
            playWaveTrack(ready);
        } else {
            fetchWaveTrack().then((track) => {
                if (track && state.wave.active) {
                    rememberCurrentForWave();
                    playWaveTrack(track);
                }
            });
        }
        return;
    }

    if (auto && state.repeat === 'one') {
        audio.currentTime = 0;
        const promise = audio.play();
        if (promise && typeof promise.catch === 'function') promise.catch(() => {});
        return;
    }

    let index;
    if (state.shuffle && state.queue.length > 1) {
        do {
            index = Math.floor(Math.random() * state.queue.length);
        } while (index === state.queueIndex);
    } else {
        index = state.queueIndex + 1;
        if (index >= state.queue.length) {
            if (!auto || state.repeat === 'all') {
                index = 0;
            } else {
                audio.pause();
                audio.currentTime = 0;
                state.playing = false;
                updatePlayer();
                return;
            }
        }
    }
    playTrack(state.queue[index], state.queue, index);
}

function prevTrack() {
    if (!state.current || !state.queue.length) return;
    if (audio.currentTime > 3) {
        audio.currentTime = 0;
        return;
    }
    let index = state.queueIndex - 1;
    if (index < 0) {
        index = state.shuffle && state.queue.length > 1
            ? Math.floor(Math.random() * state.queue.length)
            : state.queue.length - 1;
    }
    playTrack(state.queue[index], state.queue, index);
}

function seekBy(seconds) {
    if (!state.current || !Number.isFinite(audio.duration)) return;
    audio.currentTime = Math.min(Math.max(0, audio.currentTime + seconds), audio.duration);
}

async function toggleLike(id) {
    const trackId = Number(id);
    if (!trackId || state.likeBusy.has(trackId)) return;
    state.likeBusy.add(trackId);

    try {
        const updated = await request(`/api/tracks/${trackId}/like`, { method: 'POST' });

        const trackIndex = state.tracks.findIndex((item) => item.id === updated.id);
        if (trackIndex >= 0) state.tracks[trackIndex] = updated;
        if (state.current?.id === updated.id) state.current = updated;
        const queueIndex = state.queue.findIndex((item) => item.id === updated.id);
        if (queueIndex >= 0) state.queue[queueIndex] = updated;

        renderAll();
        updatePlayer();
    } catch (error) {
        toast(`Не удалось изменить коллекцию: ${error.message}`, 'error');
    } finally {
        state.likeBusy.delete(trackId);
    }
}

// ---------------------------------------------------------------------------
// Текст песни (караоке)
// ---------------------------------------------------------------------------
function parseSyncedLyrics(source) {
    const linePattern = /^((?:\[\d{1,3}:\d{2}(?:\.\d+)?\])+)\s*(.*)$/;
    const stampPattern = /\[(\d{1,3}):(\d{2}(?:\.\d+)?)\]/g;
    const lines = [];
    for (const raw of String(source || '').split(/\r?\n/)) {
        const match = raw.trim().match(linePattern);
        if (!match) continue;
        const text = match[2].trim();
        for (const stamp of match[1].match(stampPattern) || []) {
            const [minutes, seconds] = stamp.slice(1, -1).split(':');
            lines.push({ time: Number(minutes) * 60 + Number(seconds), text });
        }
    }
    return lines.sort((a, b) => a.time - b.time);
}

function updateLyricsHighlight(force = false) {
    const panel = $('#lyricsPanel');
    if (panel.classList.contains('hidden')) return;
    const { lines, nodes } = state.lyrics;
    if (!lines.length) return;

    const time = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
    let index = -1;
    for (let i = 0; i < lines.length; i += 1) {
        if (lines[i].time <= time + 0.15) index = i;
        else break;
    }
    if (!force && index === state.lyrics.activeIndex) return;

    state.lyrics.activeIndex = index;
    nodes.forEach((node, i) => node.classList.toggle('active', i === index));
    if (index >= 0) nodes[index].scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function renderLyrics(payload, trackId) {
    const body = $('#lyricsBody');
    body.replaceChildren();
    state.lyrics = { trackId, lines: [], nodes: [], activeIndex: -1, text: '' };

    const synced = String(payload?.synced_lyrics || '');
    const plain = String(payload?.plain_lyrics || '');

    if (synced) state.lyrics.lines = parseSyncedLyrics(synced);
    if (state.lyrics.lines.length) {
        for (const line of state.lyrics.lines) {
            const node = el('p', 'lyric-line', { text: line.text || '♪' });
            state.lyrics.nodes.push(node);
            body.append(node);
        }
        updateLyricsHighlight(true);
    } else if (plain) {
        state.lyrics.text = plain;
        for (const line of plain.split(/\r?\n/)) {
            body.append(el('p', 'lyric-plain', { text: line || ' ' }));
        }
    } else {
        body.append(el('p', 'text-sm text-white/35', { text: 'Текст этого трека не найден' }));
    }
}

async function toggleLyrics() {
    const panel = $('#lyricsPanel');
    if (!panel.classList.contains('hidden')) {
        closeLyrics();
        return;
    }
    if (!state.current) {
        toast('Сначала выберите трек');
        return;
    }

    const track = state.current;
    setShown(panel, true);
    $('#btnLyrics').classList.add('active');
    $('#btnLyrics').setAttribute('aria-pressed', 'true');
    $('#lyricsTitle').textContent = `${track.title || 'Без названия'}${track.artist ? ' — ' + track.artist : ''}`;
    $('#lyricsBody').replaceChildren(el('p', 'text-sm text-white/35', { text: 'Ищем текст…' }));
    state.lyrics = { trackId: track.id, lines: [], nodes: [], activeIndex: -1, text: '' };

    try {
        const payload = await request(`/api/tracks/${track.id}/lyrics`);
        if (!state.current || state.current.id !== track.id) return;
        renderLyrics(payload || {}, track.id);
    } catch (error) {
        if (!panel.classList.contains('hidden')) {
            $('#lyricsBody').replaceChildren(el('p', 'text-sm text-white/35', {
                text: `Не удалось загрузить текст: ${error.message}`,
            }));
        }
    }
}

function closeLyrics() {
    setShown($('#lyricsPanel'), false);
    $('#btnLyrics').classList.remove('active');
    $('#btnLyrics').setAttribute('aria-pressed', 'false');
    state.lyrics.activeIndex = -1;
}

// ---------------------------------------------------------------------------
// «Моя волна»
// ---------------------------------------------------------------------------
function updateWaveUI() {
    const button = $('#heroWave');
    button.classList.toggle('active', state.wave.active);
    button.setAttribute('aria-pressed', String(state.wave.active));
    const label = button.querySelector('span');
    if (label) {
        label.textContent = state.wave.loading
            ? 'Волна загружается…'
            : state.wave.active ? 'Волна включена' : 'Моя волна';
    }
}

function rememberCurrentForWave() {
    const track = state.current;
    if (!track) return;
    const values = [];
    if (track.youtube_id) values.push(String(track.youtube_id));
    if (track.artist || track.title) values.push(`${track.artist || ''} - ${track.title || ''}`);
    for (const value of values) {
        if (value && !state.wave.exclude.includes(value)) state.wave.exclude.push(value);
    }
}

async function fetchWaveTrack() {
    if (state.wave.loading) return null;
    state.wave.loading = true;
    updateWaveUI();
    try {
        const track = await request('/api/wave/next', {
            method: 'POST',
            body: JSON.stringify({
                seed_track_id: state.current?.id ?? null,
                exclude_youtube_ids: state.wave.exclude.slice(),
            }),
        });
        return state.wave.active ? track : null;
    } catch (error) {
        toast(`Волна: ${error.message}`, 'error');
        return null;
    } finally {
        state.wave.loading = false;
        updateWaveUI();
    }
}

function playWaveTrack(track) {
    state.wave.nextTrack = null;
    if (state.wave.preloadAudio) {
        state.wave.preloadAudio.pause();
        state.wave.preloadAudio = null;
    }
    playTrack(track, [track], 0);
    toast(`Волна: «${track.title || 'Трек'}»`);
}

async function startWave() {
    if (state.wave.active) {
        stopWave();
        return;
    }
    state.wave.active = true;
    state.wave.exclude = [];
    state.wave.nextTrack = null;
    updateWaveUI();
    toast('Волна включена — подбираем трек…');

    const track = await fetchWaveTrack();
    if (!track || !state.wave.active) {
        if (state.wave.active) stopWave();
        return;
    }
    rememberCurrentForWave();
    playWaveTrack(track);
}

function stopWave() {
    if (!state.wave.active && !state.wave.nextTrack) return;
    state.wave.active = false;
    state.wave.nextTrack = null;
    if (state.wave.preloadAudio) {
        state.wave.preloadAudio.pause();
        state.wave.preloadAudio = null;
    }
    state.wave.loading = false;
    updateWaveUI();
    toast('Волна выключена');
}

async function prefetchWaveTrack() {
    if (!state.wave.active || state.wave.nextTrack || state.wave.loading) return;
    if (state.wave.prefetchFor === state.current?.id) return; // не спамим при ошибке
    state.wave.prefetchFor = state.current?.id ?? null;
    rememberCurrentForWave();
    const track = await fetchWaveTrack();
    if (!track || !state.wave.active || state.wave.nextTrack) return;

    // Держим готовый аудиопоток в отдельном <audio>: переход будет мгновенным.
    state.wave.nextTrack = track;
    const preload = new Audio();
    preload.preload = 'auto';
    preload.src = track.stream_url || `/api/stream/${track.id}`;
    preload.load();
    state.wave.preloadAudio = preload;
}

function maybePrefetchWave() {
    if (!state.wave.active || state.wave.nextTrack || state.wave.loading || !state.current) return;
    const duration = Number.isFinite(audio.duration)
        ? audio.duration
        : (Number(state.current.duration) || 0);
    if (!duration || !Number.isFinite(audio.currentTime)) return;
    const remaining = duration - audio.currentTime;
    if (audio.currentTime >= duration * 0.5 || remaining <= 20) {
        prefetchWaveTrack();
    }
}

// ---------------------------------------------------------------------------
// Обновление плеера и прогресса
// ---------------------------------------------------------------------------
function updatePlayer() {
    const track = state.current;

    if (!track) {
        setImage($('#playerCover'), null);
        $('#playerTitle').textContent = 'Ничего не играет';
        $('#playerArtist').textContent = 'Выберите трек из списка';
        $('#playerLike').dataset.id = '0';
        $('#playerLike').innerHTML = icon('heart', 'w-5 h-5');
        $('#playerLike').classList.remove('liked');
    } else {
        setImage($('#playerCover'), track.cover_url);
        $('#playerTitle').textContent = track.title || 'Без названия';
        $('#playerArtist').textContent = track.artist || 'Неизвестный исполнитель';
        const like = $('#playerLike');
        like.dataset.id = String(track.id);
        like.innerHTML = icon(isLiked(track) ? 'heart-fill' : 'heart', 'w-5 h-5');
        like.classList.toggle('liked', isLiked(track));
        like.setAttribute('aria-label', isLiked(track) ? 'Убрать из коллекции' : 'Добавить в коллекцию');
    }

    $('#iconPlay').classList.toggle('hidden', state.playing);
    $('#iconPause').classList.toggle('hidden', !state.playing);
    $('#btnPlay').setAttribute('aria-label', state.playing ? 'Пауза' : 'Воспроизвести');
    $('#btnShuffle').classList.toggle('active', state.shuffle);
    $('#btnRepeat').classList.toggle('active', state.repeat !== 'off');
    $('#repeatIcon').querySelector('use').setAttribute('href', state.repeat === 'one' ? '#i-repeat-one' : '#i-repeat');

    updateProgress();
}

function updateProgress() {
    const duration = Number.isFinite(audio.duration) && audio.duration > 0
        ? audio.duration
        : (Number(state.current?.duration) || 0);
    const current = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;

    $('#currentTime').textContent = formatTime(current);
    $('#duration').textContent = formatTime(duration);

    for (const input of [$('#progress'), $('#progressMobile')]) {
        input.max = String(duration || 1);
        input.value = String(Math.min(current, duration || 1));
        setRangeFill(input);
    }

    if ('mediaSession' in navigator && state.playing && duration > 0) {
        try {
            navigator.mediaSession.setPositionState({ duration, position: current, playbackRate: 1 });
        } catch (_) { /* некоторые браузеры не поддерживают позицию */ }
    }
}

function setVolume(value) {
    state.volume = Math.min(1, Math.max(0, value));
    if (state.volume > 0 && state.muted) state.muted = false;
    updateVolumeUI();
    try { window.localStorage.setItem('music-volume', String(state.volume)); } catch (_) { /* приватный режим */ }
}

function updateVolumeUI() {
    audio.volume = state.volume;
    audio.muted = state.muted;
    const percent = Math.round(state.volume * 100);
    $('#volume').value = String(percent);
    setRangeFill($('#volume'));
    $('#volumeIcon').querySelector('use').setAttribute('href', state.muted || state.volume === 0 ? '#i-mute' : '#i-volume');
    $('#btnMute').setAttribute('aria-label', state.muted ? 'Включить звук' : 'Выключить звук');
}

// ---------------------------------------------------------------------------
// Media Session API (системный плеер, наушники, экран блокировки)
// ---------------------------------------------------------------------------
let mediaSessionHandlersReady = false;
let mediaSessionTrackId = null;

function artworkForTrack(track) {
    if (!track.cover_url) {
        return [{ src: PLACEHOLDER_URL, sizes: 'any', type: 'image/svg+xml' }];
    }
    const src = new URL(track.cover_url, window.location.origin).href;
    const ext = (track.cover_url.split('?')[0].split('.').pop() || '').toLowerCase();
    const type = ext === 'webp' ? 'image/webp'
        : ext === 'png' ? 'image/png' : 'image/jpeg';
    return [{ src, sizes: '512x512', type }];
}

function updateMediaSession() {
    if (!('mediaSession' in navigator)) return;

    // Обработчики читают живое состояние (state.queue / state.wave), поэтому
    // остаются актуальными при любом переключении трека — включая волну.
    // На всякий случай гарантируем, что они зарегистрированы.
    if (!mediaSessionHandlersReady) setupMediaSession();

    if (!state.current) {
        navigator.mediaSession.metadata = null;
        navigator.mediaSession.playbackState = 'none';
        mediaSessionTrackId = null;
        return;
    }

    const track = state.current;
    try {
        navigator.mediaSession.metadata = new MediaMetadata({
            title: track.title || 'Без названия',
            artist: track.artist || 'Неизвестный исполнитель',
            album: 'Моя музыка',
            artwork: artworkForTrack(track),
        });
    } catch (_) { /* MediaMetadata может быть недоступен */ }
    navigator.mediaSession.playbackState = state.playing ? 'playing' : 'paused';

    // Новая дорожка начинается с нуля: не переносим позицию предыдущей.
    if (mediaSessionTrackId !== track.id) {
        mediaSessionTrackId = track.id;
        const duration = Number.isFinite(audio.duration) && audio.duration > 0
            ? audio.duration
            : (Number(track.duration) || 0);
        if (duration > 0) {
            try {
                navigator.mediaSession.setPositionState({ duration, position: 0, playbackRate: 1 });
            } catch (_) { /* позиция поддерживается не везде */ }
        }
    }
}

function setupMediaSession() {
    if (!('mediaSession' in navigator)) return;
    const handlers = {
        play: () => { if (audio.paused && state.current) togglePlay(); },
        pause: () => audio.pause(),
        // Управление с гарнитуры и с экрана блокировки.
        previoustrack: () => prevTrack(),
        nexttrack: () => nextTrack(),
        seekto: (details) => {
            if (typeof details.seekTime === 'number') audio.currentTime = details.seekTime;
        },
        seekbackward: (details) => seekBy(-(details.seekOffset || 10)),
        seekforward: (details) => seekBy(details.seekOffset || 10),
    };
    for (const [action, handler] of Object.entries(handlers)) {
        try { navigator.mediaSession.setActionHandler(action, handler); } catch (_) { /* действие не поддерживается */ }
    }
    mediaSessionHandlersReady = true;
}

// ---------------------------------------------------------------------------
// Установка приложения (PWA / APK / iOS)
// ---------------------------------------------------------------------------
function detectPlatform() {
    const ua = navigator.userAgent || '';
    if (/android/i.test(ua)) return { key: 'android', label: 'Android' };
    if (/iphone|ipad|ipod/i.test(ua)
        || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) {
        return { key: 'ios', label: 'iPhone / iPad' };
    }
    return {
        key: 'desktop',
        label: /windows/i.test(ua) ? 'Windows / Desktop' : 'Desktop',
    };
}

function updateInstallUi() {
    const direct = $('#installDirectSection');
    if (direct) setShown(direct, Boolean(deferredPrompt));
    const desktop = $('#installDesktopSection');
    if (desktop) setShown(desktop, detectPlatform().key === 'desktop' && !deferredPrompt);
    $$('[data-action="open-install"]').forEach((button) => {
        button.classList.toggle('can-install', Boolean(deferredPrompt));
    });
}

function setupInstallPrompt() {
    // Событие приходит только в защищённом контексте (HTTPS) и только если
    // выполнены требования PWA: манифест, иконки и зарегистрированный SW.
    window.addEventListener('beforeinstallprompt', (event) => {
        event.preventDefault();          // не даём браузеру показать свой баннер
        deferredPrompt = event;          // сохраняем, чтобы показать свою кнопку
        updateInstallUi();
    });
    window.addEventListener('appinstalled', () => {
        deferredPrompt = null;
        updateInstallUi();
        closeInstallModal();
        toast('Приложение установлено. Приятного прослушивания!');
    });
}

async function checkApkAvailability() {
    const hint = $('#apkHint');
    const link = $('#apkDownload');
    try {
        // HEAD проверяет наличие файла, не скачивая сам APK.
        const response = await fetch(APK_CHECK_URL, { method: 'HEAD', cache: 'no-store' });
        const contentType = response.headers.get('content-type') || '';
        apkReady = response.ok && !contentType.includes('json');
    } catch (_) {
        apkReady = false;
    }
    if (link) link.classList.toggle('apk-disabled', !apkReady);
    if (hint) {
        hint.textContent = apkReady
            ? 'Сборка готова — файл скачается сразу после нажатия.'
            : 'APK пока не собран. Установите приложение в 1 клик через браузер — это быстрее.';
    }
}

function openInstallModal() {
    const modal = $('#installModal');
    if (!modal) return;
    installLastFocus = document.activeElement;
    const platform = detectPlatform();
    const label = $('#installPlatform');
    if (label) label.textContent = platform.label;

    setShown($('#installApkSection'), platform.key === 'android');
    setShown($('#installIosSection'), platform.key === 'ios');
    setShown($('#installDesktopSection'), platform.key === 'desktop' && !deferredPrompt);
    setShown($('#installDirectSection'), Boolean(deferredPrompt));

    const shareUrl = $('#shareUrl');
    if (shareUrl) shareUrl.value = window.location.href.split('#')[0];

    modal.classList.remove('hidden');
    modal.classList.add('flex');
    document.body.style.overflow = 'hidden';
    checkApkAvailability();
    window.requestAnimationFrame(() => {
        $('#installModal button[data-action="close-install"]')?.focus();
    });
}

function closeInstallModal() {
    const modal = $('#installModal');
    if (!modal || modal.classList.contains('hidden')) return;
    modal.classList.add('hidden');
    modal.classList.remove('flex');
    document.body.style.overflow = '';
    if (installLastFocus && typeof installLastFocus.focus === 'function') {
        installLastFocus.focus();
    }
}

function installPwa() {
    if (!deferredPrompt) {
        toast('Автоустановка недоступна здесь: откройте меню браузера → «Установить приложение».');
        return;
    }
    // Вызываем именно сохранённое событие — это и есть установка в 1 клик.
    deferredPrompt.prompt();
    const promptEvent = deferredPrompt;
    deferredPrompt = null;
    if (promptEvent.userChoice && typeof promptEvent.userChoice.then === 'function') {
        promptEvent.userChoice.then((choice) => {
            toast(choice?.outcome === 'accepted' ? 'Приложение устанавливается…' : 'Установка отменена');
        }).catch(() => { /* браузер мог отклонить повторный запрос */ });
    }
    updateInstallUi();
}

function handleApkClick(link, event) {
    if (apkReady) {
        // Файл есть: идём через умный роут, который отдаёт APK с нужными заголовками.
        link.href = link.dataset.downloadUrl || APK_CHECK_URL || APK_FALLBACK_PATH;
        return; // обычный переход по ссылке
    }
    event.preventDefault();
    toast('APK ещё не собран. Установите приложение в 1 клик через браузер.');
    if (deferredPrompt) installPwa();
}

async function copyShareLink() {
    const input = $('#shareUrl');
    const url = (input?.value || window.location.href).split('#')[0];
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(url);
        } else {
            // Fallback для HTTP-контекста и старых браузеров.
            const temp = el('textarea', '', { attrs: { readonly: '' } });
            temp.value = url;
            temp.style.position = 'fixed';
            temp.style.opacity = '0';
            document.body.append(temp);
            temp.select();
            const copied = document.execCommand('copy');
            temp.remove();
            if (!copied) throw new Error('copy failed');
        }
        toast('Ссылка скопирована — отправьте её другу');
    } catch (_) {
        if (input) { input.focus(); input.select(); }
        toast('Не получилось скопировать автоматически — выделите ссылку вручную', 'error');
    }
}

// ---------------------------------------------------------------------------
// События
// ---------------------------------------------------------------------------
function bindEvents() {
    document.addEventListener('click', (event) => {
        // Клик вне меню плейлистов закрывает его, не мешая кликам внутри.
        if (playlistPicker && !event.target.closest('.playlist-picker')) {
            closePlaylistPicker();
        }
        const target = event.target.closest('[data-action]');
        if (!target) return;
        const { action } = target.dataset;

        switch (action) {
            case 'view':
                setView(target.dataset.view);
                if (target.dataset.view === 'search') focusSearch();
                break;
            case 'clear-search':
                clearSearch();
                $('#searchInput').focus();
                break;
            case 'hero-play': {
                const first = state.tracks[0];
                if (first) playTrack(first, state.tracks, 0);
                break;
            }
            case 'wave':
                startWave();
                break;
            case 'open-install':
                openInstallModal();
                break;
            case 'close-install':
                closeInstallModal();
                break;
            case 'install-pwa':
                installPwa();
                break;
            case 'download-apk':
                handleApkClick(target, event);
                break;
            case 'copy-link':
                copyShareLink();
                break;
            case 'lyrics':
                toggleLyrics();
                break;
            case 'close-lyrics':
                closeLyrics();
                break;
            case 'create-playlist':
                createPlaylist(target.dataset.trackId || null);
                break;
            case 'open-playlist':
                openPlaylist(target.dataset.id);
                break;
            case 'delete-playlist':
                deletePlaylist(target.dataset.id);
                break;
            case 'add-to-playlist':
                openPlaylistPicker(Number(target.dataset.id), target);
                break;
            case 'picker-add':
                addTrackToPlaylist(target.dataset.playlistId, target.dataset.id);
                break;
            case 'picker-create':
                createPlaylist(target.dataset.id);
                break;
            case 'delete-track':
                removeTrack(target.dataset.id);
                break;
            case 'mobile-playlists':
                openMobilePlaylists();
                break;
            case 'close-mobile-playlists':
                closeMobilePlaylists();
                break;
            case 'track':
            case 'row-play': {
                const id = Number(target.dataset.id);
                const index = Number(target.dataset.index ?? -1);
                playTrackById(id, state.visible, index);
                break;
            }
            case 'like':
                toggleLike(target.dataset.id);
                break;
            case 'candidate':
                downloadCandidate(Number(target.dataset.index));
                break;
            case 'toggle':
                togglePlay();
                break;
            case 'prev':
                prevTrack();
                break;
            case 'next':
                nextTrack();
                break;
            case 'shuffle':
                state.shuffle = !state.shuffle;
                updatePlayer();
                break;
            case 'repeat':
                state.repeat = state.repeat === 'off' ? 'all' : state.repeat === 'all' ? 'one' : 'off';
                updatePlayer();
                break;
            case 'mute':
                state.muted = !state.muted;
                updateVolumeUI();
                break;
            default:
                break;
        }
    });

    const searchInput = $('#searchInput');
    searchInput.addEventListener('input', () => {
        state.query = searchInput.value;
        state.candidates = [];
        state.loading.clear();
        state.remoteError = '';
        state.searchSeq += 1;
        state.searchController?.abort();
        if (state.searchTimer) window.clearTimeout(state.searchTimer);

        setShown($('#clearSearch'), Boolean(searchInput.value));
        if (!state.query.trim()) {
            state.remoteLoading = false;
            renderAll();
            return;
        }
        if (state.view !== 'search') setView('search');
        else renderSearch();
        scheduleSearch();
    });

    searchInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            if (state.searchTimer) window.clearTimeout(state.searchTimer);
            state.searchTimer = null;
            runSearch();
        } else if (event.key === 'Escape') {
            clearSearch();
        }
    });

    $('#progress').addEventListener('input', (event) => {
        const value = Number(event.target.value);
        audio.currentTime = value;
        $('#currentTime').textContent = formatTime(value);
        setRangeFill(event.target);
        setRangeFill($('#progressMobile'));
    });
    $('#progressMobile').addEventListener('input', (event) => {
        const value = Number(event.target.value);
        audio.currentTime = value;
        $('#currentTime').textContent = formatTime(value);
        setRangeFill(event.target);
        setRangeFill($('#progress'));
    });
    $('#volume').addEventListener('input', (event) => setVolume(Number(event.target.value) / 100));

    document.addEventListener('keydown', (event) => {
        if (event.code === 'Space') {
            const tag = event.target.tagName;
            const isInteractive = event.target.isContentEditable
                || tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON';
            if (isInteractive) return; // пробел в поле ввода или на кнопке — не переключаем плеер
            event.preventDefault();
            togglePlay();
            return;
        }
        if (event.key === 'Escape') {
            closePlaylistPicker();
            closeMobilePlaylists();
            closeLyrics();
            closeInstallModal();
            return;
        }
        if (event.key === 'Enter' && (
            event.target.classList?.contains('track-row')
            || event.target.classList?.contains('candidate')
            || event.target.classList?.contains('playlist-item')
        )) {
            event.target.click();
        }
    });

    $('#playerCover').addEventListener('error', () => {
        const img = $('#playerCover');
        if (img.dataset.fallback !== '1') {
            img.dataset.fallback = '1';
            img.src = PLACEHOLDER_URL;
        }
    });

    audio.addEventListener('timeupdate', () => {
        updateProgress();
        updateLyricsHighlight();
        maybePrefetchWave();
    });
    audio.addEventListener('loadedmetadata', updateProgress);
    audio.addEventListener('durationchange', updateProgress);
    audio.addEventListener('play', () => {
        state.playing = true;
        updatePlayer();   // иконка паузы в плеере
        renderAll();      // эквалайзер в строке текущего трека
        updateMediaSession();
    });
    audio.addEventListener('pause', () => {
        state.playing = false;
        updatePlayer();
        renderAll();
        updateMediaSession();
    });
    audio.addEventListener('ended', () => nextTrack(true));
    audio.addEventListener('error', () => {
        // code 1 = MEDIA_ERR_ABORTED (обычно при быстром переключении трека).
        if (!audio.src || audio.error?.code === 1) return;
        state.playing = false;
        updatePlayer();
        toast('Не удалось воспроизвести аудио', 'error');
    });

    // Некоторые мобильные браузеры приглушают <audio> при уходе в фон.
    // Возобновляем воспроизведение, если пользователь его не останавливал.
    document.addEventListener('visibilitychange', () => {
        if (document.hidden || !state.playing || !state.current || !audio.paused) return;
        const promise = audio.play();
        if (promise && typeof promise.catch === 'function') promise.catch(() => {});
    });
}

// ---------------------------------------------------------------------------
// Запуск
// ---------------------------------------------------------------------------
async function init() {
    bindEvents();
    setupMediaSession();

    try {
        const saved = Number(window.localStorage.getItem('music-volume'));
        if (Number.isFinite(saved) && saved >= 0 && saved <= 1) state.volume = saved;
    } catch (_) { /* приватный режим */ }
    audio.volume = state.volume;
    updateVolumeUI();

    try {
        const tracks = await request('/api/tracks');
        state.tracks = Array.isArray(tracks) ? tracks : [];
    } catch (error) {
        toast(`Не удалось загрузить библиотеку: ${error.message}`, 'error');
    }

    await loadPlaylists();
    updateWaveUI();
    updateInstallUi();
    renderAll();
    updatePlayer();
}

// Слушатель beforeinstallprompt должен быть установлен как можно раньше —
// браузер выбрасывает событие сразу после загрузки страницы.
setupInstallPrompt();
init();
