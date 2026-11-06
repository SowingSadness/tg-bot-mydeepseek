"""HTTP API мини-аппа сравнения: JSON сравнения и оригиналы картинок.

Приложение собирается одной функцией `build_webapp_app`: всё, что нужно
роутам (кеш, путь оригиналов, токен бота, подписи, доступ по ссылке, статика),
передаётся аргументами и живёт в замыканиях. Глобального состояния нет, поэтому
в тестах можно поднять сколько угодно независимых приложений.

Контракт:

* ``GET /healthz`` → 200 ``ok``;
* ``GET /api/cmp/<token>`` → 403 при битой подписи токена, 401 без валидного
  ``initData`` (заголовок ``Authorization: tma <initData>`` или query
  ``?init_data=``), 404 для неизвестного номера, 403 чужому пользователю
  (если ``share_by_id=False``); иначе JSON ``{id, labels, aspect, width,
  height, images}`` и ``Cache-Control: no-store``;
* ``GET /app/cmp/img/<token>/<side>`` → 403 при битой подписи, 404 для
  неизвестной стороны/отсутствующего файла; иначе AVIF при
  ``Accept: image/avif`` (с фолбэком на WebP), ``Vary: Accept``,
  ``Cache-Control: immutable`` и ``ETag``; ``If-None-Match`` → 304 без тела;
* ``GET /app/cmp`` и ``/app/cmp/`` → ``static_dir/cmp.html`` или 404.

Безопасность: значение ``side`` допускается только ``a``/``b``, номер
сравнения берётся из проверенной подписи (только цифры), путь к файлу строится
в `bot.compare.originals`, поэтому path traversal невозможен. Токены и
``initData`` не логируются, клиенту не отдаются трейсбеки — только JSON
``{"error": ...}``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from aiohttp import web

from ..compare import originals, webauth
from ..compare.build import _height_for

log = logging.getLogger(__name__)

#: Заголовок Telegram WebApp с `initData`: `Authorization: tma <initData>`.
AUTH_HEADER = "Authorization"
#: Схема авторизации Telegram WebApp (сравниваем без учёта регистра).
AUTH_SCHEME = "tma"
#: Имя query-параметра с `initData` (запасной способ для отладки/старых клиентов).
INIT_DATA_PARAM = "init_data"
#: Имя файла страницы мини-аппа внутри `static_dir`.
PAGE_FILE = "cmp.html"
#: Общие заголовки кэширования оригиналов: они неизменяемы для номера.
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
#: Сколько символов sha256 оставляем в `ETag` (достаточно и коротко).
ETAG_LEN = 32


def _error(status: int, message: str) -> web.Response:
    """JSON-ошибка без трейсбека: клиент видит только код и короткий текст."""
    return web.json_response(
        {"error": message}, status=status, headers={"Cache-Control": "no-store"}
    )


def _init_data(request: web.Request) -> str | None:
    """`initData` из заголовка `Authorization: tma …` или query `?init_data=`.

    Заголовок — стандартный способ Telegram WebApp; query оставлен как
    запасной (и для проверки вручную). Пустое значение — это «нет данных».
    """
    header = request.headers.get(AUTH_HEADER, "")
    scheme, sep, value = header.partition(" ")
    if sep and scheme.strip().lower() == AUTH_SCHEME:
        value = value.strip()
        if value:
            return value
    query = (request.query.get(INIT_DATA_PARAM) or "").strip()
    return query or None


def _user_id(user: dict) -> int | None:
    """`user.id` из `initData` как int; отсутствие/мусор — `None`."""
    raw = user.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _etag_matches(header: str | None, etag: str) -> bool:
    """Совпадает ли `If-None-Match` с нашим `ETag` (включая `*` и слабые теги)."""
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == "*" or candidate == etag:
            return True
    return False


def _image_headers(content_type: str, etag: str) -> dict[str, str]:
    """Заголовки оригинала: формат, `Vary`, вечный кэш и `ETag`."""
    return {
        "Content-Type": content_type,
        "Vary": "Accept",
        "Cache-Control": IMMUTABLE_CACHE,
        "ETag": etag,
    }


def build_webapp_app(
    *,
    store,
    cache_dir,
    bot_token: str,
    labels: tuple[str, str] = ("До", "После"),
    share_by_id: bool = False,
    static_dir=None,
) -> web.Application:
    """Собирает aiohttp-приложение мини-аппа поверх готовых зависимостей.

    ``store`` — `bot.compare.store.ComparisonStore` (нужен только `get`);
    ``cache_dir`` — каталог кеша, внутри которого `originals` ищет файлы;
    ``bot_token`` — токен бота для проверки `initData`;
    ``labels`` — запасные подписи, если в записи кеша их не оказалось;
    ``share_by_id`` — разрешать просмотр любому, кто знает номер (ссылка-доступ);
    ``static_dir`` — каталог со страницей `cmp.html` (нет файла — 404, шаг 6).

    Зависимости захвачены замыканиями: глобального состояния у приложения нет.
    """

    async def healthz(request: web.Request) -> web.Response:
        """Живость сервера: без БД, диска и авторизации."""
        return web.Response(text="ok", content_type="text/plain")

    async def comparison(request: web.Request) -> web.Response:
        """JSON сравнения после проверки токена, `initData` и владельца."""
        cmp_id = webauth.verify(request.match_info["token"])
        if cmp_id is None:
            return _error(403, "Ссылка недействительна")

        init_data = _init_data(request)
        if init_data is None:
            return _error(401, "Откройте сравнение из Telegram")
        parsed = webauth.parse_init_data(init_data, bot_token)
        if parsed is None:
            return _error(401, "Не удалось проверить данные Telegram")

        try:
            cached = await store.get(cmp_id)
        except Exception:  # noqa: BLE001 — клиенту нужен код, а не трейсбек
            log.exception("Не удалось прочитать сравнение из кеша")
            return _error(500, "Кеш сравнений недоступен")
        if cached is None:
            return _error(404, "Сравнение не найдено")

        if not webauth.owner_allowed(
            _user_id(parsed["user"]), cached.owner_id, share_by_id
        ):
            return _error(403, "Это сравнение создано другим пользователем")

        try:
            token = webauth.sign(cached.id)
            width = max(1, int(cached.width))
            payload = {
                "id": cached.id,
                "labels": list(cached.labels or labels),
                "aspect": cached.aspect,
                "width": width,
                "height": _height_for(width, cached.aspect),
                "images": {
                    "a": f"/app/cmp/img/{token}/a",
                    "b": f"/app/cmp/img/{token}/b",
                },
            }
        except Exception:  # noqa: BLE001 — битая запись не должна давать трейсбек
            log.exception("Не удалось собрать ответ для сравнения #%s", cached.id)
            return _error(500, "Сравнение повреждено")
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def image(request: web.Request) -> web.Response:
        """Оригинал стороны: AVIF по `Accept`, иначе WebP; `ETag` → 304."""
        cmp_id = webauth.verify(request.match_info["token"])
        if cmp_id is None:
            return _error(403, "Ссылка недействительна")

        side = request.match_info["side"]
        if side not in originals.SIDES:
            return _error(404, "Нет такой картинки")

        try:
            found = await asyncio.to_thread(
                originals.read,
                cache_dir,
                cmp_id,
                side,
                accept=request.headers.get("Accept", ""),
            )
        except Exception:  # noqa: BLE001 — чтение диска не должно отдавать трейсбек
            log.exception("Не удалось прочитать оригинал #%s (%s)", cmp_id, side)
            return _error(500, "Не удалось прочитать картинку")
        if found is None:
            return _error(404, "Нет такой картинки")

        blob, fmt = found
        content_type = originals.AVIF_MIME if fmt == "avif" else originals.WEBP_MIME
        etag = '"' + hashlib.sha256(blob).hexdigest()[:ETAG_LEN] + '"'
        headers = _image_headers(content_type, etag)
        if _etag_matches(request.headers.get("If-None-Match"), etag):
            # 304 без тела: браузер берёт картинку из своего кэша.
            return web.Response(status=304, headers=headers)
        return web.Response(body=blob, headers=headers)

    async def page(request: web.Request) -> web.Response:
        """Статическая страница мини-аппа; без `static_dir`/файла — 404."""
        if static_dir is None:
            return _error(404, "Страница мини-аппа не настроена")
        path = Path(static_dir) / PAGE_FILE
        try:
            if not path.is_file():
                return _error(404, "Страница мини-аппа не найдена")
            return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
        except OSError:
            return _error(404, "Страница мини-аппа не найдена")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/cmp/{token}", comparison)
    app.router.add_get("/app/cmp/img/{token}/{side}", image)
    app.router.add_get("/app/cmp", page)
    app.router.add_get("/app/cmp/", page)
    return app
