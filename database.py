"""database.py — асинхронная работа с SQLite (aiosqlite).

Таблицы:
    tracks          — метаданные скачанных треков (обратная совместимость);
    playlists       — пользовательские плейлисты;
    playlist_tracks — связь «плейлист ↔ трек» с каскадным удалением.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite

# Корень проекта (папка music_service) — рядом с этим файлом.
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'music.db'

# Единственное подключение к БД на весь процесс. aiosqlite выполняет все
# запросы в одном фоновом потоке, поэтому конкурентные вызовы из FastAPI
# безопасны и сериализуются автоматически.
_db: aiosqlite.Connection | None = None


def _to_dict(row: sqlite3.Row | None) -> dict | None:
    """Преобразует sqlite3.Row в dict (или возвращает None для пустого результата)."""
    return dict(row) if row is not None else None


async def init_db() -> None:
    """Создаёт файл БД и все таблицы, если их ещё нет.

    Вызывается один раз при старте сервера (lifespan в main.py).
    Существующая таблица `tracks` никогда не пересоздаётся — она остаётся
    совместимой с базой предыдущих этапов.
    """
    global _db
    if _db is not None:
        return

    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = sqlite3.Row

    # WAL-режим позволяет читать БД во время записи — удобно для стриминга.
    await _db.execute('PRAGMA journal_mode = WAL;')
    await _db.execute('PRAGMA foreign_keys = ON;')

    await _db.execute(
        '''
        CREATE TABLE IF NOT EXISTS tracks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            artist      TEXT    NOT NULL,
            duration    INTEGER NOT NULL DEFAULT 0,
            file_path   TEXT    NOT NULL,
            cover_path  TEXT,
            youtube_id  TEXT    UNIQUE,
            is_liked    BOOLEAN NOT NULL DEFAULT 0,
            play_count  INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        '''
    )

    # Плейлисты. `created_at` имеет DEFAULT CURRENT_TIMESTAMP — вставлять
    # дату вручную не требуется.
    await _db.execute(
        '''
        CREATE TABLE IF NOT EXISTS playlists (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        '''
    )

    # Связующая таблица. PRIMARY KEY (playlist_id, track_id) гарантирует,
    # что один трек не может быть добавлен в плейлист дважды. Внешние ключи
    # с ON DELETE CASCADE автоматически удаляют связи при удалении плейлиста
    # или трека (PRAGMA foreign_keys = ON выставлен выше).
    await _db.execute(
        '''
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist_id INTEGER NOT NULL,
            track_id    INTEGER NOT NULL,
            added_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (playlist_id, track_id),
            FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        );
        '''
    )
    await _db.commit()


async def close_db() -> None:
    """Закрывает подключение к БД (при остановке сервера)."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# --------------------------------------------------------------------------
# Запросы
# --------------------------------------------------------------------------


async def get_track(track_id: int) -> dict | None:
    """Возвращает трек по id или None."""
    cur = await _db.execute('SELECT * FROM tracks WHERE id = ?', (track_id,))
    return _to_dict(await cur.fetchone())


async def get_track_by_youtube_id(youtube_id: str) -> dict | None:
    """Возвращает трек по youtube_id или None (защита от дублей при скачивании)."""
    cur = await _db.execute('SELECT * FROM tracks WHERE youtube_id = ?', (youtube_id,))
    return _to_dict(await cur.fetchone())


async def get_all_tracks() -> list[dict]:
    """Все треки библиотеки, сначала — самые новые (сортировка по новизне)."""
    cur = await _db.execute('SELECT * FROM tracks ORDER BY created_at DESC, id DESC')
    return [dict(row) for row in await cur.fetchall()]


async def search_tracks(query: str) -> list[dict]:
    """Поиск по названию или исполнителю без учёта регистра.

    Важно: встроенный SQLite LIKE не умеет сравнивать кириллицу без учёта
    регистра, поэтому фильтрация выполняется в Python через str.casefold() —
    так поиск одинаково хорошо работает и для латиницы, и для кириллицы.
    """
    needle = query.strip().casefold()
    all_tracks = await get_all_tracks()
    return [
        track for track in all_tracks
        if needle in track['title'].casefold() or needle in track['artist'].casefold()
    ]


async def add_track(
    title: str,
    artist: str,
    duration: int,
    file_path: str,
    cover_path: str | None,
    youtube_id: str,
) -> dict:
    """Добавляет трек в БД и возвращает его полную запись (dict)."""
    cur = await _db.execute(
        '''
        INSERT INTO tracks (title, artist, duration, file_path, cover_path, youtube_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (title, artist, duration, file_path, cover_path, youtube_id),
    )
    await _db.commit()
    return await get_track(cur.lastrowid)


async def toggle_like(track_id: int) -> dict | None:
    """Переключает is_liked (0 <-> 1) и возвращает обновлённый трек."""
    await _db.execute('UPDATE tracks SET is_liked = NOT is_liked WHERE id = ?', (track_id,))
    await _db.commit()
    return await get_track(track_id)


async def increment_play_count(track_id: int) -> None:
    """Увеличивает счётчик прослушиваний на 1 (вызывается при стриминге)."""
    await _db.execute('UPDATE tracks SET play_count = play_count + 1 WHERE id = ?', (track_id,))
    await _db.commit()


# --------------------------------------------------------------------------
# Плейлисты
# --------------------------------------------------------------------------


async def create_playlist(name: str) -> dict:
    """Создаёт плейлист и возвращает его запись."""
    cur = await _db.execute(
        'INSERT INTO playlists (name) VALUES (?)', (name.strip(),)
    )
    await _db.commit()
    cur = await _db.execute('SELECT * FROM playlists WHERE id = ?', (cur.lastrowid,))
    return dict(await cur.fetchone())


async def get_playlists() -> list[dict]:
    """Список плейлистов с количеством треков в каждом ( track_count )."""
    cur = await _db.execute(
        '''
        SELECT p.id, p.name, p.created_at, COUNT(pt.track_id) AS track_count
        FROM playlists AS p
        LEFT JOIN playlist_tracks AS pt ON pt.playlist_id = p.id
        GROUP BY p.id
        ORDER BY p.created_at DESC, p.id DESC
        '''
    )
    return [dict(row) for row in await cur.fetchall()]


async def get_playlist(playlist_id: int) -> dict | None:
    """Возвращает плейлист по id или None."""
    cur = await _db.execute('SELECT * FROM playlists WHERE id = ?', (playlist_id,))
    return _to_dict(await cur.fetchone())


async def get_playlist_tracks(playlist_id: int) -> list[dict]:
    """Треки плейлиста в порядке добавления (сначала новые)."""
    cur = await _db.execute(
        '''
        SELECT t.* FROM tracks AS t
        JOIN playlist_tracks AS pt ON pt.track_id = t.id
        WHERE pt.playlist_id = ?
        ORDER BY pt.added_at DESC, pt.rowid DESC
        ''',
        (playlist_id,),
    )
    return [dict(row) for row in await cur.fetchall()]


async def add_track_to_playlist(playlist_id: int, track_id: int) -> bool | None:
    """Добавляет трек в плейлист.

    Возвращает True при успехе (в том числе если трек уже был в плейлисте —
    INSERT OR IGNORE не создаст дубликат) и None, если плейлист или трек
    не существуют.
    """
    if await get_playlist(playlist_id) is None or await get_track(track_id) is None:
        return None
    await _db.execute(
        '''
        INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id)
        VALUES (?, ?)
        ''',
        (playlist_id, track_id),
    )
    await _db.commit()
    return True


async def remove_track_from_playlist(playlist_id: int, track_id: int) -> bool:
    """Убирает трек из плейлиста. Возвращает True, если запись была удалена."""
    cur = await _db.execute(
        'DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?',
        (playlist_id, track_id),
    )
    await _db.commit()
    return cur.rowcount > 0


async def delete_playlist(playlist_id: int) -> bool:
    """Удаляет плейлист. Связи в playlist_tracks удалятся каскадно."""
    cur = await _db.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
    await _db.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# Удаление трека
# --------------------------------------------------------------------------


def _safe_unlink(path: str | None, allowed_dir: Path) -> None:
    """Удаляет файл, только если он действительно лежит в storage-папке.

    Защита от того, чтобы значение из БД не привело к удалению
    произвольного файла вне хранилища.
    """
    if not path:
        return
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    try:
        resolved = candidate.resolve()
        allowed = allowed_dir.resolve()
        if resolved.parent != allowed and allowed not in resolved.parents:
            return
        resolved.unlink(missing_ok=True)
    except (OSError, ValueError):
        # Файл уже удалён/заблокирован — запись из БД всё равно убираем.
        pass


async def delete_track(track_id: int) -> bool:
    """Удаляет трек из БД и его MP3/обложку с диска.

    Связи с плейлистами исчезают автоматически благодаря ON DELETE CASCADE.
    Возвращает False, если трек с таким id не найден.
    """
    track = await get_track(track_id)
    if track is None:
        return False

    await _db.execute('DELETE FROM tracks WHERE id = ?', (track_id,))
    await _db.commit()

    _safe_unlink(track.get('file_path'), BASE_DIR / 'storage' / 'tracks')
    _safe_unlink(track.get('cover_path'), BASE_DIR / 'storage' / 'covers')
    return True
