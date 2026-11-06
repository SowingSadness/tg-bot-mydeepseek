@echo off
rem ---------------------------------------------------------------
rem  /cmp — локальная демка HTML-бабла по двум файлам.
rem
rem    cmp-demo.cmd <файл_до> <файл_после> [ширина 200-600] [serve^|build]
rem    либо просто перетащите два файла на этот .cmd в проводнике.
rem
rem  serve (по умолчанию) — поднимает локальный сервер и открывает браузер.
rem  build               — только собирает out\demo\widget.html + index.html.
rem
rem  Переменные: CMP_DEMO_NOPAUSE=1 — не ждать Enter в конце/на ошибке
rem              (удобно для скриптов и тестов).
rem ---------------------------------------------------------------
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

rem --- Python: сначала .venv репозитория, потом py -3, потом python
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set PY="%~dp0.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [!] Не найден Python. Создайте .venv в этом каталоге или установите Python 3.11+.
  if not defined CMP_DEMO_NOPAUSE pause
  exit /b 2
)

if "%~1"=="" goto usage
if "%~2"=="" goto usage

set "A=%~1"
set "B=%~2"
set "W=%~3"
set "MODE=%~4"
if "%MODE%"=="" set "MODE=serve"

if not exist "%A%" (
  echo [!] Нет такого файла: "%A%"
  if not defined CMP_DEMO_NOPAUSE pause
  exit /b 2
)
if not exist "%B%" (
  echo [!] Нет такого файла: "%B%"
  if not defined CMP_DEMO_NOPAUSE pause
  exit /b 2
)

set "WIDTH_ARG="
if not "%W%"=="" set "WIDTH_ARG=--width %W%"

if /i "%MODE%"=="serve" goto serve
if /i "%MODE%"=="build" goto build
if /i "%MODE%"=="--no-server" goto build
if /i "%MODE%"=="noserve" goto build

echo [!] Неизвестный режим: "%MODE%". Ожидается serve или build.
if not defined CMP_DEMO_NOPAUSE pause
exit /b 2

:serve
echo Собираю бабл и поднимаю демку:
echo   A: "%A%"
echo   B: "%B%"
echo   ширина: %W%
echo.
%PY% -m bot.compare.demo --a "%A%" --b "%B%" --out out\demo %WIDTH_ARG% --serve --open
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo [!] Демка завершилась с кодом %CODE%.
  if not defined CMP_DEMO_NOPAUSE pause
)
exit /b %CODE%

:build
echo Собираю бабл без сервера:
echo   A: "%A%"
echo   B: "%B%"
echo   ширина: %W%
echo.
%PY% -m bot.compare.demo --a "%A%" --b "%B%" --out out\demo %WIDTH_ARG% --no-server
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo [!] Сборка завершилась с кодом %CODE%.
) else (
  echo.
  echo Готово. Откройте out\demo\index.html двойным кликом.
  echo Payload для sendWidget: out\demo\widget.html
)
if not defined CMP_DEMO_NOPAUSE pause
exit /b %CODE%

:usage
echo /cmp — локальная демка HTML-бабла (шторка «до/после»).
echo.
echo Использование:
echo   cmp-demo.cmd ^<файл_до^> ^<файл_после^> [ширина 200-600] [serve^|build]
echo.
echo Примеры:
echo   cmp-demo.cmd "D:\Kir\Pictures\cmp\1.png" "D:\Kir\Pictures\cmp\1.1.png"
echo   cmp-demo.cmd "D:\Kir\Pictures\cmp\1.png" "D:\Kir\Pictures\cmp\1.1.png" 320
echo   cmp-demo.cmd "D:\Kir\Pictures\cmp\1.png" "D:\Kir\Pictures\cmp\1.1.png" 430 build
echo.
echo Можно перетащить два файла на этот .cmd в проводнике — откроется демка.
echo Собранный payload без сервера: out\demo\widget.html
if not defined CMP_DEMO_NOPAUSE pause
exit /b 1
