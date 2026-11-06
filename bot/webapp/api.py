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
* ``POST /api/cmp/log`` → 204, если клиентский отчёт об ошибке принят; 403 при
  битом/отсутствующем токене, 400 для пустого/не-JSON ``message``, 413 для
  сообщения длиннее 500 символов или тела больше 8 КБ; не больше 30 отчётов
  в минуту на токен (лишние отбрасываются с тем же 204 и одной `WARNING`-строкой
  в логе); ``message`` и ``where`` схлопываются в одну строку и проходят
  ``redact()``;
* каждый запрос даёт ровно одну строку лога `<status> <METHOD> <url> — <N> мс`
  (`INFO` < 400, `WARNING` 4xx, `ERROR` 5xx), а `POST /api/cmp/log` — всегда
  `ERROR`, даже при `204`, плюс `cmp=`/`user=`, когда известны; токен
  `startapp` в URL виден по требованию заказчика, `init_data` — нет. Это делает
  middleware `log_requests`, а не `access_log` (он остаётся `None`).

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
import re
import time
from pathlib import Path

from aiohttp import web

from ..compare import originals, webauth
from ..compare.build import _height_for
from ..logging_setup import _one_line, describe_webapp_request, redact

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
#: Предел тела `POST /api/cmp/log`: больше — 413 от aiohttp.
CLIENT_LOG_MAX_BODY = 8 * 1024
#: Максимальная длина клиентского сообщения в символах.
CLIENT_LOG_MAX_MESSAGE = 500
#: Максимальная длина поля `where` в символах.
CLIENT_LOG_MAX_WHERE = 100
#: Путь клиентских отчётов: его middleware всегда пишет на `ERROR`.
CLIENT_LOG_PATH = "/api/cmp/log"
#: Лимит клиентских отчётов в минуту на один токен.
CLIENT_LOG_LIMIT = 30
#: Окно лимита в секундах.
CLIENT_LOG_WINDOW = 60.0
#: Допустимые уровни клиентского отчёта; всё остальное — `error`.
CLIENT_LOG_LEVELS = {
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
}

#: Текст, похожий на `startapp`-токен (`<цифры>_<base64url>`): `redact()`
#: вырезает только токен бота (`<id>:<secret>`), а такой токен из клиентского
#: поля тоже не должен попадать в лог.
_TOKEN_LIKE_RE = re.compile(r"\d{1,16}_[A-Za-z0-9_-]{8,}")

#: Ключ `app` со счётчиками лимита клиентских отчётов (виден тестам).
CLIENT_LOGS_KEY: web.AppKey[dict[str, list[float]]] = web.AppKey("client_logs", dict)


def _now() -> float:
    """Монотонное время лимита; вынесено, чтобы тесты могли сдвигать окно."""
    return time.monotonic()


def _client_text(value: str, limit: int) -> str:
    """Клиентский текст для лога: без токенов, переводов строк и лишних пробелов."""
    return _one_line(redact(_TOKEN_LIKE_RE.sub("<token>", value)), limit=limit)


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


@web.middleware
async def log_requests(request: web.Request, handler) -> web.StreamResponse:
    """Одна строка о каждом запросе мини-аппа, в том числе об ошибке.

    Путь и query проходят через `describe_webapp_request` → `redact_request_url`:
    `init_data` в лог не попадает, а токен `startapp` виден по требованию
    заказчика. Статус берётся из ответа, а для `web.HTTPException` — из
    исключения; само исключение пробрасывается дальше без изменений.

    Уровень обычных запросов — по статусу (`INFO` < 400, `WARNING` 4xx,
    `ERROR` 5xx). Клиентский отчёт `POST /api/cmp/log` — это доставка ошибки
    со страницы, поэтому он пишется на `ERROR` даже при пустом `204`; номер
    сравнения и пользователь добавляются суффиксами `cmp=`/`user=`.
    """
    started = time.perf_counter()
    status = 500
    try:
        response = await handler(request)
        status = response.status
        return response
    except web.HTTPException as exc:
        status = exc.status
        raise
    except Exception:  # noqa: BLE001 — статус для лога, исключение уходит выше
        status = 500
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        is_client_log = request.method == "POST" and request.path == CLIENT_LOG_PATH
        if is_client_log:
            # Отчёт об ошибке со страницы: сам факт отчёта — уже ERROR.
            level = logging.ERROR
        elif status < 400:
            level = logging.INFO
        elif status < 500:
            level = logging.WARNING
        else:
            level = logging.ERROR
        user_id = request.get("webapp_user_id")
        # Для отчёта `user=` дописываем вместе с `cmp=` ниже, чтобы порядок
        # суффиксов был `… — N мс cmp=<id> user=<id>`.
        line = describe_webapp_request(
            request.method,
            request.path_qs,
            status,
            elapsed_ms,
            user_id=None if is_client_log else user_id,
        )
        if is_client_log:
            cmp_id = request.get("webapp_cmp_id")
            if cmp_id is not None:
                line += f" cmp={cmp_id}"
            if user_id is not None:
                line += f" user={user_id}"
        log.log(level, "%s", line)


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
    Счётчики лимита клиентских отчётов тоже живут в замыкании: у каждого
    приложения свой словарь `{sha256(token): [monotonic timestamps]}`.
    """
    client_logs: dict[str, list[float]] = {}

    def _prune_client_logs(now: float) -> None:
        """Удаляет ключи лимита, чей последний отчёт старше окна."""
        stale = [
            key
            for key, stamps in client_logs.items()
            if not stamps or now - stamps[-1] >= CLIENT_LOG_WINDOW
        ]
        for key in stale:
            client_logs.pop(key, None)

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
        # Для middleware: чей это запрос (картинки и /healthz идут без user).
        request["webapp_user_id"] = _user_id(parsed["user"])

        try:
            cached = await store.get(cmp_id)
        except Exception:  # noqa: BLE001 — клиенту нужен код, а не трейсбек
            log.exception("Не удалось прочитать сравнение #%s из кеша", cmp_id)
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

    async def client_log(request: web.Request) -> web.Response:
        """Клиентский отчёт об ошибке из самой страницы мини-аппа.

        Токен обязателен и проверяется подписью: без него в лог не пишется
        ничего. `url` (страница, где случилась ошибка) намеренно не логируем.
        Лимит — 30 отчётов в минуту на токен, сверх — `204` клиенту и одна
        `WARNING`-строка о том, что отчёт отброшен (тело отчёта не пишется).
        """
        try:
            data = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return _error(413, "Слишком большое сообщение")
        except (ValueError, web.HTTPException):
            return _error(400, "Ожидается JSON")
        if not isinstance(data, dict):
            return _error(400, "Ожидается JSON-объект")

        raw_token = data.get("token")
        cmp_id = webauth.verify(raw_token) if isinstance(raw_token, str) else None
        if cmp_id is None:
            return _error(403, "Ссылка недействительна")
        # Для middleware: `cmp=<id>` в строке запроса и в отчёте.
        request["webapp_cmp_id"] = cmp_id

        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            return _error(400, "Пустое сообщение")
        if len(message) > CLIENT_LOG_MAX_MESSAGE:
            return _error(413, "Слишком длинное сообщение")

        level_name = data.get("level")
        level = CLIENT_LOG_LEVELS.get(
            level_name.strip().lower() if isinstance(level_name, str) else "",
            logging.ERROR,
        )
        where = data.get("where")
        where = (
            _client_text(where, CLIENT_LOG_MAX_WHERE) if isinstance(where, str) else ""
        )

        key = hashlib.sha256(raw_token.encode()).hexdigest()
        now = _now()
        # Каждый вызов подчищает ключи, у которых последний отчёт уже вне окна:
        # иначе словарь лимита растёт вместе с числом разных токенов.
        _prune_client_logs(now)
        recent = [
            stamp
            for stamp in client_logs.get(key, ())
            if now - stamp < CLIENT_LOG_WINDOW
        ]
        if len(recent) >= CLIENT_LOG_LIMIT:
            client_logs[key] = recent
            # Клиенту по контракту всё равно 204, но отброшенный отчёт не должен
            # исчезать бесследно. Тело сообщения здесь не пишем: строка короткая
            # и без секретов, `where` уже прошёл `_client_text`.
            log.warning(
                "webapp[client] cmp=%s отчёт отброшен (лимит %d/мин, окно %d с) where=%s",
                cmp_id,
                CLIENT_LOG_LIMIT,
                int(CLIENT_LOG_WINDOW),
                where or "-",
            )
            return web.Response(status=204)
        recent.append(now)
        client_logs[key] = recent

        log.log(
            level,
            "webapp[client] cmp=%s where=%s message=%s",
            cmp_id,
            where or "-",
            _client_text(message, CLIENT_LOG_MAX_MESSAGE),
        )
        return web.Response(status=204)

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

    app = web.Application(client_max_size=CLIENT_LOG_MAX_BODY)
    #: Состояние лимита клиентских отчётов — видно тестам и диагностике.
    app[CLIENT_LOGS_KEY] = client_logs
    app.middlewares.append(log_requests)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/cmp/{token}", comparison)
    app.router.add_post(CLIENT_LOG_PATH, client_log)
    app.router.add_get("/app/cmp/img/{token}/{side}", image)
    app.router.add_get("/app/cmp", page)
    app.router.add_get("/app/cmp/", page)
    return app
