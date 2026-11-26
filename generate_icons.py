"""generate_icons.py — генератор иконок PWA без внешних зависимостей.

Рисует два квадратных PNG (192x192 и 512x512): тёмный фон #0e0e0e,
жёлто-оранжевый круг #ffcc00 и чёрная нота внутри. Сглаживание сделано
суперсэмплингом (3x3) и реализовано на чистом Python + zlib, поэтому
Pillow не нужен.

Запуск из папки music_service:
    python generate_icons.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / 'static'

BG = (0x0E, 0x0E, 0x0E)          # #0e0e0e
ACCENT = (0xFF, 0xCC, 0x00)      # #ffcc00
ACCENT_EDGE = (0xFF, 0x9D, 0x00)  # тёплый край круга
NOTE = (0x00, 0x00, 0x00)        # чёрная нота
SS = 3                           # суперсэмплинг: SS x SS на пиксель


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def _in_polygon(x: float, y: float, points: list[tuple[float, float]]) -> bool:
    """Классический алгоритм луча: попадание точки в многоугольник."""
    inside = False
    j = len(points) - 1
    for i in range(len(points)):
        xi, yi = points[i]
        xj, yj = points[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _in_note(x: float, y: float) -> bool:
    """Чёрная восьмая нота: наклонённая головка, штиль и флаг."""
    # Головка — эллипс, повёрнутый против часовой стрелки.
    dx, dy = x - 0.425, y - 0.625
    cos_a, sin_a = math.cos(-0.42), math.sin(-0.42)
    ex = dx * cos_a - dy * sin_a
    ey = dx * sin_a + dy * cos_a
    if (ex / 0.125) ** 2 + (ey / 0.088) ** 2 <= 1.0:
        return True

    # Штиль.
    if 0.520 <= x <= 0.562 and 0.255 <= y <= 0.625:
        return True

    # Флаг: изогнутый язычок от верха штиля.
    flag = [
        (0.552, 0.250), (0.640, 0.272), (0.710, 0.320), (0.742, 0.395),
        (0.706, 0.367), (0.665, 0.345), (0.612, 0.330), (0.552, 0.335),
    ]
    return _in_polygon(x, y, flag)


def _pixel_color(x: float, y: float) -> tuple[int, int, int]:
    """Цвет в нормализованных координатах (0..1) внутри иконки."""
    # Диск с лёгким градиентом к тёплому краю.
    dx, dy = x - 0.5, y - 0.48
    dist = math.hypot(dx, dy)
    if dist <= 0.355:
        if _in_note(x, y):
            return NOTE
        # К центру ярче, к краю — оранжевее.
        return _mix(ACCENT, ACCENT_EDGE, max(0.0, (dist - 0.16) / 0.24) * 0.55)
    return BG


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack('>I', len(data))
        + kind
        + data
        + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def render(size: int) -> bytes:
    """Возвращает готовые байты PNG заданного размера."""
    step = 1.0 / (size * SS)
    raw = bytearray()
    for py in range(size):
        raw.append(0)  # фильтр PNG: None
        for px in range(size):
            acc = [0, 0, 0]
            for sy in range(SS):
                y = (py + (sy + 0.5) / SS) / size
                for sx in range(SS):
                    x = (px + (sx + 0.5) / SS) / size
                    color = _pixel_color(x, y)
                    for channel in range(3):
                        acc[channel] += color[channel]
            samples = SS * SS
            raw.extend((c // samples for c in acc))

    ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)  # RGB, 8 bit
    return (
        b'\x89PNG\r\n\x1a\n'
        + _chunk(b'IHDR', ihdr)
        + _chunk(b'IDAT', zlib.compress(bytes(raw), level=9))
        + _chunk(b'IEND', b'')
    )


def main() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        path = STATIC_DIR / f'icon-{size}.png'
        path.write_bytes(render(size))
        print(f'[OK] {path.name}: {size}x{size}, {path.stat().st_size} bytes')


if __name__ == '__main__':
    main()
