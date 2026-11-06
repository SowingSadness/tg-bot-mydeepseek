"""Локальная демка бабла сравнения: файлы, мок Telegram и мини-сервер.

Демка собирает бабл **тем же** `build.build`, что и бот, кладёт рядом ровно тот HTML,
который уйдёт в `sendWidget` (`widget.html`), и страницу-«телефон» (`index.html`)
с переключателем ширины 200–600 px, светлой/тёмной темой и мок-`TelegramWidget`.

Зачем сервер: в `file://` iframe получает непрозрачный origin, `localStorage` в нём
бросает исключение, и виджет не запоминает положение шторки. Мини-сервер на
`http.server` отдаёт страницу и виджет с одного origin, поэтому `localStorage` работает.

    python -m bot.compare.demo --a before.jpg --b after.jpg
    python -m bot.compare.demo --a before.jpg --b after.jpg --serve --open
    python -m bot.compare.demo --a before.jpg --b after.jpg --out out/demo --no-server

Модуль намеренно не импортирует `bot.config`: демка должна запускаться без `.env`,
поэтому все параметры сборки — собственные дефолты, совпадающие с ботовскими.
"""

from __future__ import annotations

import argparse
import functools
import html
import json
import sys
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import build, images

#: Ширина превью по умолчанию (телефонный максимум), как у бота.
DEMO_WIDTH = 430
#: Границы слайдера ширины в демке.
MIN_WIDTH = 200
MAX_WIDTH = 600
#: Порт мини-сервера по умолчанию: при занятости берётся следующий свободный.
DEMO_PORT = 8765
PORT_ATTEMPTS = 20

TITLE = "Сравнение"
HINT = "Проведите ↔"
LABELS = ("До", "После")

#: Параметры сборки: те же значения, что бот передаёт в `build.build`.
BUILD_OPTIONS = {
    "start_width": 430,
    "min_width": 160,
    "start_quality": 34,
    "min_quality": 12,
    "margin": 64,
    "title": TITLE,
    "hint": HINT,
}

#: Мок Telegram WebApp: ровно те поля, которые читает шаблон, плюс тема и лог запросов.
#: Скрипт ставится ПЕРЕД разметкой бабла — и в srcdoc, и в отдаваемом `/widget.html`.
_MOCK_JS = r"""
(function () {
  "use strict";
  var palettes = {
    light: {
      bg: "#ffffff",
      text: "#000000",
      hint: "#707579",
      link: "#3390ec",
      button: "#3390ec",
      buttonText: "#ffffff",
      secondaryBg: "#f4f4f5"
    },
    dark: {
      bg: "#212121",
      text: "#ffffff",
      hint: "#aaaaaa",
      link: "#5ca5f0",
      button: "#5ca5f0",
      buttonText: "#ffffff",
      secondaryBg: "#181818"
    }
  };
  var events = {};

  function themeParams(name) {
    var palette = palettes[name] || palettes.dark;
    return {
      bg_color: palette.bg,
      text_color: palette.text,
      hint_color: palette.hint,
      link_color: palette.link,
      button_color: palette.button,
      button_text_color: palette.buttonText,
      secondary_bg_color: palette.secondaryBg
    };
  }

  function fire(name, payload) {
    (events[name] || []).forEach(function (callback) {
      try {
        callback(payload);
      } catch (error) {
        console.error("[mock TelegramWidget]", name, error);
      }
    });
  }

  function apply(name) {
    var palette = palettes[name] || palettes.dark;
    var style = document.documentElement.style;
    style.setProperty("--tg-theme-bg-color", palette.bg);
    style.setProperty("--tg-theme-text-color", palette.text);
    style.setProperty("--tg-theme-hint-color", palette.hint);
    style.setProperty("--tg-theme-link-color", palette.link);
    style.setProperty("--tg-theme-button-color", palette.button);
    style.setProperty("--tg-theme-button-text-color", palette.buttonText);
    style.setProperty("--tg-theme-secondary-bg-color", palette.secondaryBg);
    style.setProperty("--tg-theme-text-padding", "8px");
    style.setProperty("color-scheme", name);
    style.background = palette.bg;
    document.documentElement.setAttribute("data-theme", name);
  }

  var widget = {
    colorScheme: "dark",
    themeParams: themeParams("dark"),
    contentSafeAreaInset: { top: 0, right: 0, bottom: 0, left: 0 },
    safeAreaInset: { top: 0, right: 0, bottom: 0, left: 0 },
    viewportHeight: window.innerHeight,
    viewportStableHeight: window.innerHeight,
    isExpanded: true,
    initData: "",
    ready: function () {
      console.log("[mock TelegramWidget] ready");
    },
    expand: function () {},
    close: function () {},
    onEvent: function (name, callback) {
      (events[name] = events[name] || []).push(callback);
    },
    offEvent: function (name, callback) {
      var list = events[name] || [];
      var index = list.indexOf(callback);
      if (index >= 0) {
        list.splice(index, 1);
      }
    },
    HapticFeedback: {
      impactOccurred: function (style) {
        console.log("[mock TelegramWidget] impact", style);
      },
      notificationOccurred: function (type) {
        console.log("[mock TelegramWidget] notification", type);
      },
      selectionChanged: function () {
        console.log("[mock TelegramWidget] selection");
      }
    },
    request: function (url, data) {
      console.log("[mock TelegramWidget] request", url, data);
      return Promise.resolve({ status: 200, ok: true });
    },
    showAlert: function (text) {
      console.log("[mock TelegramWidget] alert", text);
    }
  };

  window.TelegramWidget = widget;
  // Мостик для страницы-«телефона»: она переключает тему в iframe (same-origin).
  window.__tgSetTheme = function (name) {
    var next = palettes[name] ? name : "dark";
    widget.colorScheme = next;
    widget.themeParams = themeParams(next);
    apply(next);
    fire("themeChanged", { themeParams: widget.themeParams });
  };
  apply("dark");
})();
"""

_INDEX_CSS = """
:root{
  --page-bg:#0f1216;--page-fg:#e9edf2;--page-muted:#98a2ad;
  --card:#1a1f26;--line:#2b323c;
}
html[data-theme=light]{
  --page-bg:#eef1f5;--page-fg:#1b1f24;--page-muted:#5b636c;
  --card:#ffffff;--line:#d7dce2;
}
*{box-sizing:border-box}
body{
  margin:0;min-height:100vh;padding:24px 16px 40px;display:flex;flex-direction:column;
  align-items:center;gap:18px;background:var(--page-bg);color:var(--page-fg);
  font:14px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif;
  transition:background .15s,color .15s;
}
header{display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center}
h1{margin:0;font-size:18px;font-weight:600}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:center}
.controls label{display:flex;gap:8px;align-items:center;color:var(--page-muted)}
.controls input[type=range]{width:180px;accent-color:#5ca5f0}
.controls output{min-width:52px;font-variant-numeric:tabular-nums;color:var(--page-fg)}
button{
  padding:7px 12px;border:1px solid var(--line);border-radius:9px;
  background:var(--card);color:var(--page-fg);font:inherit;cursor:pointer;
}
button:hover{border-color:#5ca5f0}
.phone{
  padding:12px;border-radius:22px;background:var(--card);border:1px solid var(--line);
  box-shadow:0 10px 40px #0004;max-width:100%;overflow:auto;
}
.phone iframe{border:0;border-radius:12px;background:#111;display:block;max-width:100%}
.caption{margin:0;font-variant-numeric:tabular-nums}
.errors{margin:0;color:var(--page-muted);font-size:12px;font-variant-numeric:tabular-nums}
.note{margin:0;max-width:640px;color:var(--page-muted);font-size:12px;text-align:center}
@media (max-width:720px){body{padding:16px 10px 32px}.phone{padding:8px;border-radius:16px}}
"""

_INDEX_PAGE = """<!doctype html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Демка бабла — __TITLE__</title>
<style>__CSS__</style>
</head>
<body>
<header>
  <h1>Бабл сравнения — локальная демка</h1>
  <div class="controls">
    <label>Ширина
      <input id="width" type="range" min="__MIN__" max="__MAX__" step="1" value="__WIDTH__">
      <output id="width-value">__WIDTH__px</output>
    </label>
    <button id="theme" type="button">Тема: тёмная</button>
    <button id="reload" type="button">Перезагрузить</button>
  </div>
</header>
<main>
  <div class="phone">__FRAME__</div>
  <p class="caption" id="caption">__CAPTION__</p>
  <p class="errors" id="errors">__ERRORS__</p>
  <p class="note">Мок Telegram: тема, HapticFeedback и request пишутся в консоль iframe.
  Нативный рендерер клиента демка не заменяет.</p>
</main>
<script>
(function () {
  "use strict";
  var AR = __AR__;
  var frame = document.getElementById("frame");
  var slider = document.getElementById("width");
  var output = document.getElementById("width-value");
  var caption = document.getElementById("caption");
  var themeButton = document.getElementById("theme");
  var reloadButton = document.getElementById("reload");
  var CAPTION = __CAPTION_JS__;
  var dark = true;

  function applySize(value) {
    var width = Math.min(__MAX__, Math.max(__MIN__, Math.round(+value || __WIDTH__)));
    var height = Math.round(width / AR);
    frame.style.width = width + "px";
    frame.style.height = height + "px";
    frame.setAttribute("width", width);
    frame.setAttribute("height", height);
    slider.value = width;
    output.textContent = width + "px";
    caption.textContent = CAPTION.replace("{w}", width);
  }

  function syncTheme() {
    try {
      var view = frame.contentWindow;
      if (view && view.__tgSetTheme) {
        view.__tgSetTheme(dark ? "dark" : "light");
      }
    } catch (error) {
      console.warn("[demo] тема не применилась", error);
    }
  }

  function applyTheme() {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    themeButton.textContent = dark ? "Тема: тёмная" : "Тема: светлая";
    syncTheme();
  }

  slider.addEventListener("input", function () { applySize(slider.value); });
  themeButton.addEventListener("click", function () { dark = !dark; applyTheme(); });
  reloadButton.addEventListener("click", function () {
    try {
      frame.contentWindow.location.reload();
    } catch (error) {
      frame.src = frame.src;
    }
  });
  frame.addEventListener("load", function () { applySize(slider.value); syncTheme(); });

  applySize(slider.value);
  applyTheme();
})();
</script>
</body>
</html>
"""


class DemoRequestHandler(SimpleHTTPRequestHandler):
    """Отдаёт `index.html` как есть, а `widget.html` — с мок-скриптом в начале."""

    server_version = "cmp-demo/1.0"

    def do_GET(self) -> None:  # noqa: N802 - имя задано stdlib
        if urlsplit(self.path).path.rstrip("/") in ("/widget.html", "/widget"):
            self._send_widget()
            return
        super().do_GET()

    def _send_widget(self) -> None:
        target = Path(self.directory) / "widget.html"
        try:
            widget = target.read_text(encoding="utf-8")
        except OSError:
            self.send_error(404, "widget.html not found")
            return
        self._send_html(with_mock(widget))

    def _send_html(self, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - имя задано stdlib
        print(f"[demo] {self.address_string()} {format % args}", file=sys.stderr)


def _escape(value: str) -> str:
    """HTML-экранирование текста и значений атрибутов (`&`, `<`, `>`, `"`, `'`)."""
    return html.escape(str(value), quote=True)


def _js_string(value: str) -> str:
    """Строковый литерал JS: JSON плюс защита от `</script>` внутри значения."""
    return json.dumps(str(value), ensure_ascii=False).replace("</", "<\\/")


def mock_script() -> str:
    """Мок `TelegramWidget` как готовый `<script>` — ставится первым в документе."""
    return "<script>" + _MOCK_JS + "</script>\n"


def with_mock(widget_html: str) -> str:
    """Виджет с мок-Telegram перед всем текстом (для сервера и `srcdoc`)."""
    return mock_script() + widget_html


def srcdoc(widget_html: str) -> str:
    """Значение атрибута `srcdoc`: мок + виджет, экранированные целиком."""
    return _escape(with_mock(widget_html))


def aspect_ratio(aspect: str) -> float:
    """`'3:2'` → `1.5`; на непонятном значении — дефолт 3:2 (демка не должна падать)."""
    left, _, right = str(aspect).partition(":")
    try:
        width, height = float(left), float(right)
    except ValueError:
        return 3 / 2
    if width <= 0 or height <= 0:
        return 3 / 2
    return width / height


def clamp_width(value: int) -> int:
    """Держит ширину превью в границах слайдера, предупреждая о подмене."""
    width = int(value)
    if MIN_WIDTH <= width <= MAX_WIDTH:
        return width
    fixed = min(MAX_WIDTH, max(MIN_WIDTH, width))
    print(
        f"Ширина {width}px вне {MIN_WIDTH}–{MAX_WIDTH} — использую {fixed}px.",
        file=sys.stderr,
    )
    return fixed


def parse_labels(value: str | None) -> tuple[str, str]:
    """`'До|После'` → пара подписей; на мусоре — стандартные, без падения."""
    parts = [part.strip() for part in str(value or "").split("|")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        print(
            f'Подписи «{value}» не в формате "До|После" — беру стандартные.',
            file=sys.stderr,
        )
        return LABELS
    return parts[0], parts[1]


def build_widget(
    img_a,
    img_b,
    labels: tuple[str, str] = LABELS,
) -> build.Built:
    """Собирает бабл теми же параметрами, что бот (без `bot.config`)."""
    return build.build(img_a, img_b, labels=labels, **BUILD_OPTIONS)


def caption(built: build.Built, width: int | str = DEMO_WIDTH) -> str:
    """Подпись под баблом: ширина превью и то, во что обошёлся HTML."""
    return (
        f"{width}px · zlib {built.zlib_bytes}/{build.Z_LIMIT} "
        f"· q{built.quality_a}/{built.quality_b} "
        f"· {built.profile_a}/{built.profile_b}"
    )


def details(built: build.Built) -> str:
    """Вторая строка подписи: ошибки, байты и реально выбранная ширина сборки."""
    return (
        f"err_a {built.err_a:.4f} · err_b {built.err_b:.4f} "
        f"· raw {built.html_bytes} B / {build.RAW_LIMIT} "
        f"· pictures {built.picture_bytes} B "
        f"· собрано {built.width}px, AR {built.aspect}"
    )


def index_html(
    built: build.Built,
    *,
    width: int = DEMO_WIDTH,
    serve: bool = False,
) -> str:
    """Страница-«телефон»: контролы, iframe с баблом и подпись с размерами.

    `serve=False` — iframe с `srcdoc` (страница самодостаточна, открывается файлом);
    `serve=True` — iframe на `widget.html` (мок добавит сервер, same-origin для `localStorage`).
    """
    width = clamp_width(width)
    ratio = aspect_ratio(built.aspect)
    height = round(width / ratio)

    frame = (
        f'<iframe id="frame" title="Бабл сравнения" width="{width}" height="{height}" '
        f'style="width:{width}px;height:{height}px;border:0;display:block"'
    )
    if serve:
        frame += ' src="widget.html"></iframe>'
    else:
        frame += f' srcdoc="{srcdoc(built.html)}"></iframe>'

    page = _INDEX_PAGE
    replacements = (
        ("__TITLE__", _escape(TITLE)),
        ("__CSS__", _INDEX_CSS),
        ("__AR__", f"{ratio:.6g}"),
        ("__WIDTH__", str(width)),
        ("__MIN__", str(MIN_WIDTH)),
        ("__MAX__", str(MAX_WIDTH)),
        ("__FRAME__", frame),
        ("__CAPTION__", _escape(caption(built, "{w}"))),
        ("__CAPTION_JS__", _js_string(caption(built, "{w}"))),
        ("__ERRORS__", _escape(details(built))),
    )
    for placeholder, value in replacements:
        page = page.replace(placeholder, value)
    return page


def _report(
    built: build.Built,
    out_dir: Path,
    width: int,
    labels: tuple[str, str],
) -> None:
    """Печатает размеры сборки, включая примерный вклад шаблона и картинок."""
    template_html = build.render_cached(
        image_a=b"",
        image_b=b"",
        labels=labels,
        title=TITLE,
        hint=HINT,
        aspect=built.aspect,
    )
    template_z = build.zsize(template_html)
    pictures_z = max(0, built.zlib_bytes - template_z)

    print("Собрано демо-бабло:")
    print(f"  width={built.width} (превью {width}px, aspect {built.aspect})")
    print(f"  quality_a={built.quality_a} quality_b={built.quality_b}")
    print(f"  profile_a={built.profile_a} profile_b={built.profile_b}")
    print(
        f"  zlib={built.zlib_bytes}/{build.Z_LIMIT} "
        f"(шаблон ~{template_z} + картинки ~{pictures_z})"
    )
    print(f"  raw={built.html_bytes}/{build.RAW_LIMIT} pictures={built.picture_bytes}")
    print(f"  err_a={built.err_a:.4f} err_b={built.err_b:.4f}")
    print(f"  widget: {out_dir / 'widget.html'}")
    print(f"  index:  {out_dir / 'index.html'}")


def make_server(directory: str | Path, port: int = 0) -> ThreadingHTTPServer:
    """Собирает HTTP-сервер демки из каталога `directory` (0 — свободный порт)."""
    handler = functools.partial(DemoRequestHandler, directory=str(directory))
    return ThreadingHTTPServer(("127.0.0.1", int(port)), handler)


def bind_server(
    directory: str | Path,
    port: int = DEMO_PORT,
    attempts: int = PORT_ATTEMPTS,
) -> ThreadingHTTPServer:
    """Занят порт? Берём следующий свободный — но только для дефолтного порта."""
    last: OSError | None = None
    for candidate in range(int(port), int(port) + max(1, int(attempts))):
        try:
            return make_server(directory, candidate)
        except OSError as exc:
            last = exc
    if last is None:  # недостижимо: range всегда даёт хотя бы одну попытку
        raise OSError("не удалось подобрать порт для демки")
    raise last


def _serve(out_dir: Path, *, port: int | None, open_browser: bool) -> int:
    """Поднимает мини-сервер до Ctrl+C и печатает адрес и способ остановки."""
    try:
        server = make_server(out_dir, port) if port is not None else bind_server(out_dir)
    except OSError as exc:
        print(f"Не удалось занять порт демки: {exc}", file=sys.stderr)
        return 1

    host, actual_port = server.server_address[:2]
    url = f"http://{host}:{actual_port}/"
    print(f"Демка открыта: {url}", flush=True)
    print("Остановить: Ctrl+C", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.", flush=True)
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI демки: собрать бабл, записать файлы и (по `--serve`) поднять сервер."""
    parser = argparse.ArgumentParser(
        prog="python -m bot.compare.demo",
        description="Локальная демка HTML-бабла сравнения: файлы, мок Telegram, мини-сервер.",
    )
    parser.add_argument("--a", required=True, help="первая картинка (нижний слой)")
    parser.add_argument("--b", required=True, help="вторая картинка (верхний слой)")
    parser.add_argument(
        "--labels",
        default="|".join(LABELS),
        help='подписи слоёв через "|", по умолчанию "До|После"',
    )
    parser.add_argument(
        "--width",
        type=int,
        default=DEMO_WIDTH,
        help=f"ширина превью в браузере, px ({MIN_WIDTH}–{MAX_WIDTH})",
    )
    parser.add_argument(
        "--out",
        default="out/demo",
        help="каталог для widget.html и index.html (по умолчанию out/demo)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--serve",
        action="store_true",
        help="поднять мини-сервер (same-origin нужен для localStorage виджета)",
    )
    mode.add_argument(
        "--no-server",
        action="store_true",
        help="только файлы, сервер не поднимать (страница откроется двойным кликом)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"порт мини-сервера (по умолчанию {DEMO_PORT}, при занятости — следующий)",
    )
    parser.add_argument("--open", action="store_true", help="открыть страницу в браузере")
    args = parser.parse_args(argv)

    labels = parse_labels(args.labels)
    width = clamp_width(args.width)

    try:
        img_a = images.load(Path(args.a).read_bytes())
        img_b = images.load(Path(args.b).read_bytes())
    except OSError as exc:
        print(f"Не удалось прочитать файл: {exc}", file=sys.stderr)
        return 2
    except images.ImageError as exc:
        print(f"Картинка не подошла: {exc}", file=sys.stderr)
        return 2

    try:
        built = build_widget(img_a, img_b, labels)
    except build.WidgetTooLarge as exc:
        print(f"Не удалось собрать бабл: {exc}", file=sys.stderr)
        print(
            "Демка ничего не записала. Попробуйте картинки попроще или меньше по размеру.",
            file=sys.stderr,
        )
        return 2
    except images.ImageError as exc:
        print(f"Картинка не подошла: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # widget.html — РОВНО payload для sendWidget: без мока и без обёрток.
        # `newline="\n"` обязателен: иначе Windows превратит \n в \r\n и файл
        # перестанет совпадать с тем, что уходит в `sendWidget`.
        (out_dir / "widget.html").write_text(
            built.html, encoding="utf-8", newline="\n"
        )
        (out_dir / "index.html").write_text(
            index_html(built, width=width, serve=args.serve),
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        print(f"Не удалось записать файлы демки: {exc}", file=sys.stderr)
        return 2

    _report(built, out_dir, width, labels)

    if not args.serve:
        print(f"Готово: откройте {out_dir / 'index.html'} двойным кликом (сервер не нужен).")
        return 0

    return _serve(out_dir, port=args.port, open_browser=args.open)


if __name__ == "__main__":
    raise SystemExit(main())
