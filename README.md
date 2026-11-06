# tg-bot-mydeepseek

Телеграм-бот на **DeepSeek API** (модель `deepseek-flash`). Работает в двух режимах:

- **Inline** — в любом чате наберите `@username ваш запрос`, бот вернёт ответ, который можно отправить в чат. Если в ответе есть ссылки на изображения, они показываются галереей — тапните, чтобы вставить картинку (по аналогии с `@pic`).
- **Личные сообщения и группы** — обычный диалог с ботом. Пришлите **фото** (или фото с подписью-вопросом, или ответьте на фото) — бот разберёт изображение. Ссылку на картинку можно указать и прямо в тексте запроса.
- **Сравнение картинок (`/cmp`)** — бот собирает интерактивный HTML-бабл Telegram: две картинки со шторкой «до/после» прямо в сообщении. Каждое сравнение получает номер, сжатые картинки остаются в кеше бота, а в любом чате можно набрать `@username #номер` и собрать тот же бабл из кеша.

Контекст диалога не сохраняется: каждый запрос независим. Режим рассуждений выключен по умолчанию и включается командой `/think`.

## Команды

| Команда | Действие |
| --- | --- |
| `/start` | Приветствие |
| `/help` | Справка |
| `/cmp` | Сравнить две картинки: ссылки, два фото или ответ на фото |
| `/think` | Включить/выключить режим рассуждений (для сложных задач) |
| `/reset` | Сбросить настройки |

## Требования

- Python 3.11+ (или Docker)
- Токен бота от [@BotFather](https://t.me/BotFather)
- Ключ DeepSeek API с [platform.deepseek.com](https://platform.deepseek.com) с положительным балансом

### Настройка бота в @BotFather

1. Создайте бота и получите токен: `/newbot`.
2. Включите inline-режим (обязательно!): `/setinline` → укажите подсказку, например `Поиск ответа…`.
3. Разрешите добавление в группы: `/setjoingroups` → **Enable**.
4. По желанию: `/setinlinefeedback` → статистика использования inline-режима.

> Юзернейм бота должен совпадать со значением `BOT_USERNAME` в `.env` — по нему бот понимает, когда его упомянули в группе. Значение `BOT_USERNAME` задаётся при развороте и обязательно к заполнению; можно писать как `name`, так и `@name`.

## Быстрый старт (локально)

```bash
git clone git@github.com:SowingSadness/tg-bot-mydeepseek.git
cd tg-bot-mydeepseek

python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements.txt

cp .env.example .env      # Windows: copy .env.example .env
# откройте .env и впишите TELEGRAM_BOT_TOKEN, DEEPSEEK_API_KEY и BOT_USERNAME

python -m bot.main
```

## Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Тесты не ходят в сеть: Bot API и скачивание картинок подменяются двойниками, кеш — настоящий
SQLite во временном каталоге. Покрыты сборка бабла и лимиты, центр-кроп 4:3, кеш номеров,
клиент Bot API 10.4, сценарии `/cmp`, inline по `#номер` и проводка роутеров.

## Настройки (`.env`)

| Переменная | Обязательна | По умолчанию | Описание |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | да | — | Токен от @BotFather |
| `DEEPSEEK_API_KEY` | да | — | Ключ DeepSeek API |
| `DEEPSEEK_BASE_URL` | нет | `https://api.deepseek.com` | Базовый URL API |
| `DEEPSEEK_MODEL` | нет | `deepseek-flash` | Модель. Картинки умеет только `deepseek-flash` |
| `BOT_USERNAME` | **да** | — | Имя бота без `@` (например `sowing_tg_bot`), задаётся при развороте |
| `ALLOWED_USER_IDS` | нет | пусто | ID пользователей через запятую. Пусто = доступ открыт всем |
| `REQUEST_TIMEOUT` | нет | `180` | Таймаут запроса к DeepSeek в личке/группах, секунды |
| `CHAT_MAX_TOKENS` | нет | `0` | Лимит длины ответов в личке/группах. `0` = без ограничения |
| `LOG_LEVEL` | нет | `INFO` | Уровень логирования |
| `INLINE_TIMEOUT` | нет | `8.5` | Жёсткий дедлайн inline-ответа, секунды (Telegram даёт ~10) |
| `INLINE_MAX_TOKENS` | нет | `350` | Лимит длины inline-ответа. Меньше = быстрее |
| `INLINE_CACHE_TTL` | нет | `300` | TTL кэша inline-ответов (в памяти бота и в кэше Telegram), секунды |
| `INLINE_EARLY_UPDATE` | нет | `true` | Показывать частичный ответ, не дожидаясь конца генерации |
| `INLINE_EARLY_DELAY` | нет | `1.2` | Через сколько секунд показать частичный ответ |
| `INLINE_EARLY_MIN_CHARS` | нет | `120` | Либо раньше, если успело накопиться столько символов |
| `WARMUP_ON_START` | нет | `true` | Прогрев соединения с DeepSeek при старте |

### Как ускоряется inline-ответ

Inline-режим самый чувствительный к задержке: Telegram аннулирует inline-запрос
примерно через 10 секунд (`QUERY_ID_INVALID`). Что сделано для скорости:

- **прогрессивный ответ**: ответ стримится из DeepSeek, и частичный текст показывается
  сразу (`INLINE_EARLY_UPDATE`) — первую версию ответа видно за секунды, а не за 10;
  когда генерация завершается, выдача обновляется полным текстом и картинками
  (повторный `answerInlineQuery` по тому же `query_id` заменяет результаты);
- ответ ограничен по длине (`INLINE_MAX_TOKENS`) и коротким системным промптом;
- thinking-режим в inline принудительно выключен — он не влезает в дедлайн;
- устаревшие запросы отменяются: пока вы печатаете, обрабатывается только последний;
- жёсткий дедлайн `INLINE_TIMEOUT`: если время вышло, отдаётся то, что успело
  сгенерироваться, иначе — подсказка с кнопкой «Открыть чат с ботом»;
- запросы короче 3 символов не отправляются в DeepSeek;
- ответы кэшируются в памяти бота и в кэше Telegram (`INLINE_CACHE_TTL`) — повторный
  тот же запрос отдаётся мгновенно и бесплатно;
- в выдаче не более 4 картинок и **только JPEG**: превью в PNG/WebP Telegram молча
  не показывает, а каждая картинка — это ещё и загрузка превью клиентом;
- соединение с DeepSeek прогревается при старте (TLS-хендшейк не попадает в первый запрос).

### Замеры в логах

Каждый inline-запрос пишет строку вида:

```
inline[abcdef12] user=123: ok ttft=0.87s early=1.24s total=3.10s chars=412 photos=0
```

- `ttft` — время до первого токена DeepSeek;
- `early` — когда показан частичный ответ (`-1`, если не показывался);
- `total` — полное время;
- `chars` / `photos` — длина ответа и число картинок в выдаче.

Другие варианты исхода: `cache_hit` (отдали из памяти), `deadline_partial`
(время вышло, отдали частичный), `deadline_empty` (не успел ни один токен),
`empty` (модель вернула пустой ответ), `error`. Ответы в личке/группах логируются
строкой `chat user=...: total=... chars=... images=... thinking=...`.

### Как узнать свой Telegram ID

Напишите боту [@userinfobot](https://t.me/userinfobot) — он пришлёт ваш ID. Рекомендуется заполнить `ALLOWED_USER_IDS`, чтобы ботом не пользовались посторонние за ваш счёт.

## Сравнение картинок (`/cmp`)

Две картинки превращаются в **HTML-бабл** (Bot API 10.4, `sendWidget`) — картинку со шторкой
«до/после», которую можно тянуть пальцем прямо в сообщении.

1. `/cmp` — бот попросит две картинки (двумя сообщениями или альбомом); можно сразу ссылками:
   `/cmp https://a.jpg https://b.jpg`. `/cmp` в ответ на фото берёт это фото первым и ждёт второе.
2. Картинки обрезаются **по центру под 4:3**, сжимаются в WebP и запекаются в HTML. Сборка сама
   подбирает размер и качество, чтобы уложиться в лимиты Telegram: 32 768 байт HTML и 4096 байт
   после `zlib` уровня 9.
3. Сравнение получает номер (`#482913`) — он в подписи сообщения, а **сжатые картинки** остаются
   в SQLite-кеше на диске (`CMP_CACHE_DIR`).
4. В любом чате: `@username #482913` — тот же бабл, собранный из кеша: без DeepSeek, без
   скачиваний и без риска не уложиться в 10-секундный дедлайн inline-режима.
5. Если клиент не умеет виджеты, бот пришлёт обе картинки альбомом и номер текстом.

Кеш чистится по TTL и по числу записей (на старте и раз в 6 часов). Номер по умолчанию работает
только у своего владельца (`CMP_SHARE_BY_ID=false`), а не у любого, кто его подобрал.

| Переменная | По умолчанию | Описание |
| --- | --- | --- |
| `WIDGET_API_BASE` | `https://api.telegram.org/beta` | База Bot API 10.4. После выхода `sendWidget` в прод — `https://api.telegram.org` |
| `WIDGET_API_TEST_ENV` | `false` | Тестовый контур: `/bot<token>/test/<method>` |
| `CMP_CACHE_DIR` | `var/cmp` | Каталог SQLite-кеша сравнений (в Docker — том `cmp-cache`) |
| `CMP_CACHE_TTL_DAYS` | `30` | Сколько живёт сравнение |
| `CMP_CACHE_MAX_ENTRIES` | `2000` | Верхняя граница числа сравнений в кеше (~20 МБ) |
| `CMP_TARGET_WIDTH` | `430` | Ширина картинок в бабле (телефонный максимум) |
| `CMP_MIN_WIDTH` | `160` | Нижняя граница автоподгонки |
| `CMP_START_QUALITY` | `34` | Стартовое качество WebP |
| `CMP_MIN_QUALITY` | `16` | Минимальное качество WebP |
| `CMP_ZLIB_MARGIN` | `64` | Запас под лимит 4096 байт |
| `CMP_MAX_BYTES` | `8388608` | Сколько байт качать по ссылке |
| `CMP_PENDING_TTL` | `300` | Сколько ждать вторую картинку, секунды |
| `CMP_ID_DIGITS` | `6` | Длина номера сравнения |
| `CMP_SHARE_BY_ID` | `false` | `true` — номер работает у любого пользователя |
| `CMP_SHOW_HINT` | `true` | Присылать отдельную подсказку про `@bot #номер` |
| `CMP_ALLOW_PRIVATE_HOSTS` | `false` | Разрешить ссылки на приватные адреса (SSRF-защита) |
| `CMP_LABELS` | `До\|После` | Подписи слоёв бабла |
| `CMP_CAPTION` | `Сравнение #{id} · {a} / {b}` | Подпись сообщения с баблом |
| `CMP_TITLE` | `Сравнение` | `<title>` бабла |
| `CMP_HINT` | `Проведите ↔` | Подсказка внутри бабла |

> Картинки в бабле — **превью**: две фотографии укладываются примерно на 160 px, скриншоты и
> плоская графика — около 400 px. Для детального сравнения присылайте оригиналы обычными фото.

## Деплой на VPS (Debian, через Docker)

```bash
# 1. Установить Docker (если ещё нет)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # затем перелогиниться

# 2. Забрать проект и настроить окружение
sudo mkdir -p /opt/tg-bot-mydeepseek && sudo chown "$USER" /opt/tg-bot-mydeepseek
git clone git@github.com:SowingSadness/tg-bot-mydeepseek.git /opt/tg-bot-mydeepseek
cd /opt/tg-bot-mydeepseek
cp .env.example .env
nano .env                          # TOKEN, DEEPSEEK_API_KEY, BOT_USERNAME, ALLOWED_USER_IDS

# 3. Запустить
docker compose up -d --build

# 4. Проверить
docker compose logs -f
```

Обновление после `git pull`:

```bash
cd /opt/tg-bot-mydeepseek
git pull
docker compose up -d --build
```

## Деплой без Docker (systemd)

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin bot
sudo mkdir -p /opt/tg-bot-mydeepseek && sudo chown bot:bot /opt/tg-bot-mydeepseek

sudo -u bot git clone git@github.com:SowingSadness/tg-bot-mydeepseek.git /opt/tg-bot-mydeepseek
cd /opt/tg-bot-mydeepseek
sudo -u bot python3 -m venv .venv
sudo -u bot .venv/bin/pip install -r requirements.txt

sudo -u bot cp .env.example .env
sudo -u bot nano .env

sudo cp deploy/bot.service /etc/systemd/system/tg-bot-mydeepseek.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-bot-mydeepseek
sudo journalctl -u tg-bot-mydeepseek -f
```

## Структура проекта

```
bot/
├── main.py            # точка входа, роутеры, long polling, кеш и Bot API на старте
├── config.py          # настройки из .env (pydantic-settings)
├── deepseek.py        # клиент DeepSeek (текст, картинки, thinking)
├── respond.py         # общая логика ответов и скачивания фото
├── keyboards.py       # inline-клавиатура переключателя /think
├── state.py           # состояние в памяти (режим рассуждений)
├── utils.py           # Markdown→HTML, ссылки на картинки, разбивка 4096
├── compare/           # сравнение картинок: бабл, кеш, inline по номеру
│   ├── template.html  # сам бабл (шторка «до/после»)
│   ├── images.py      # 4:3 центр-кроп, WebP, скачивание по ссылке
│   ├── build.py       # сборка HTML и подгонка под лимиты Telegram
│   ├── store.py       # SQLite-кеш: номер → сжатые картинки
│   ├── api.py         # Bot API 10.4: sendWidget / answerInlineQuery
│   ├── service.py     # сценарии /cmp и ожидание второй картинки
│   └── inline.py      # "#номер" в inline-режиме
└── handlers/
    ├── private.py     # личные сообщения
    ├── group.py       # группы (по упоминанию или ответу)
    ├── inline.py      # inline-режим (@username запрос)
    └── compare.py     # роутер /cmp

tests/                 # pytest: сборка, кеш, Bot API, /cmp, inline, проводка
```

## Как это работает

- Запросы идут на OpenAI-совместимый эндпоинт DeepSeek (`/chat/completions`).
- Изображения передаются как base64 data-URL (скачанные из Telegram фото) или как внешние `http(s)`-ссылки из текста запроса.
- Режим рассуждений управляется параметром `thinking` (`extra_body`), при включении добавляется `reasoning_effort: high`.
- Inline-запросы Telegram ограничены 256 символами и не могут нести изображение — для картинок используйте личку или группу.
- В группах бот отвечает только на упоминание `@username` или на ответ (reply) своему сообщению.
- `/cmp` собирает HTML-бабл из двух WebP и отправляет его через `sendWidget` (Bot API 10.4, пока на
  бете). Номер сравнения и сжатые картинки хранятся в SQLite-кеше, поэтому inline по `#номер`
  отдаёт готовый бабл за миллисекунды.

## Ограничения

- DeepSeek **не генерирует** изображения — бот только анализирует присланные картинки и умеет вставлять ссылки на изображения, если они есть в ответе модели.
- Telegram Bot API даёт скачивать файлы до 20 МБ, поэтому очень большие фото бот не обработает.
- Ответы длиннее 4096 символов разбиваются на несколько сообщений.
- `/cmp` работает в личных сообщениях: в каналы виджеты отправлять нельзя, а в группах бот
  отвечает только на упоминание.
- Внутри бабла — только сжатые превью, оригиналы остаются в чате обычными фото.
- HTML-баблы требуют клиента Telegram 13.2+ и бетового Bot API: старые клиенты покажут заглушку
  или присланный альбом.

## Лицензия

MIT — см. [LICENSE](LICENSE).
