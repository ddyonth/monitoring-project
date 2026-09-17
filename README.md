# monitoring project

Проект для сбора и анализа активности процессов на рабочих машинах Windows.

## Что делает

- клиентский агент собирает сведения о запущенных процессах, пользователе, машине и базовых метриках активности;
- агент сохраняет локальный буфер в SQLite и отправляет данные на сервер по HTTP(S);
- сервер принимает события, хранит их в PostgreSQL и показывает данные через веб-панель;
- в серверной части есть справочники процессов и цепочек, роли машин и базовая логика оповещений;
- система помогает видеть, какие процессы запускались, на каких машинах, кем и насколько это похоже на нормальный профиль работы.

## Структура проекта

- `client/` — агент сбора активности, локальная БД `activity.db`, конфиг и файлы сборки.
- `server/` — FastAPI-сервер (хранение в PostgreSQL), HTML/JS-панель, конфиг и Dockerfile.
- `requirements.txt` — зависимости Python для клиента и сервера.

## Основные возможности

- сбор списка процессов с метаданными;
- отправка накопленных событий на сервер пакетами;
- хранение истории активности;
- отображение машин, ролей и каталогов в веб-интерфейсе;
- классификация процессов по типам через каталог;
- серверные алерты по аномальной активности и несоответствию роли.

## Технологии

- Python
- FastAPI
- PostgreSQL (сервер), SQLite (локальный буфер агента)
- psutil
- Jinja2
- PyInstaller

## Как запускать

1. Установить зависимости:

```powershell
pip install -r requirements.txt
```

2. Настроить файлы:

- `client/config.json`
- `server/config.json`

Файлы `client/config.json` и `server/config.json` необязательны: при их отсутствии используются встроенные настройки из исходного кода. Если нужно изменить адрес сервера, ключи API, интервалы или другие параметры, создайте/отредактируйте эти файлы.

3. Запустить сервер:

```powershell
python server/server_app.py
```

4. Запустить клиентский агент:

```powershell
python client/client_agent.py
```

## Клиентский агент: версия, обновление, автозапуск

### Версия и релизы

Версия агента хранится в одном файле `client/VERSION` (например `1.2`). При сборке
`client/client_agent.spec` генерирует из него модуль `_version.py` и PyInstaller
вшивает его в exe, поэтому в рантайме с диска ничего не читается. Для локальной
проверки другой версии достаточно поменять файл и пересобрать, без правки `.py`:

```bash
echo "1.3" > client/VERSION
pyinstaller client/client_agent.spec --distpath client/dist --workpath client/build --noconfirm
git checkout client/VERSION
```

Релизы хранятся на сервере в Postgres (таблица `client_releases`), текущий —
последний загруженный. Публикация (ключ `api_key`, не ключ агента):

```bash
curl -sS --fail -X POST -H "X-API-Key: $MONITORING_API_KEY" \
  -F version=1.3 -F file=@client/dist/client_agent.exe \
  http://127.0.0.1:8000/api/client-release
```

Агент читает метаданные `GET /api/client-release` и скачивает
`GET /api/download/client-agent` с заголовком `X-Client-Key`
(`client_update_key` в `client/config.json`, переопределяется переменной
`MONITORING_CLIENT_UPDATE_KEY`). Базовый адрес сервера выводится из
`server_ingest_url` (суффикс `/api/ingest` отбрасывается).

### Самообновление

Раз в цикл (после отправки данных, не в режиме `--once`) агент сравнивает версию
релиза со своей (как числа через точку, `1.10` > `1.9`). Если релиз новее:
скачивает его в `client_agent.exe.download` рядом с собой, пересчитывает sha256
и сверяет с метаданными, при совпадении переименовывает текущий exe в
`client_agent.exe.old`, ставит новый на его место, запускает его с теми же
аргументами независимо от себя и завершается. `.old` удаляется при следующем
старте. Подписи релиза нет: целостность — sha256 плюс ключ и HTTPS канала.

Ручная проверка без Планировщика: `client_agent.exe --apply-update-now` делает
одну проверку/установку и выходит (код 0 — обновлять нечего или обновлено,
1 — ошибка: sha256 не совпал, скачивание не удалось, запуск не из exe).

Важно: упакованный onefile-exe **не читает `config.json` рядом с собой**
(`__file__` в PyInstaller указывает во временный каталог), поэтому адрес сервера
берётся из встроенного значения по умолчанию, а ключи — из переменных окружения
пользователя (`setx MONITORING_API_KEY ...`, `setx MONITORING_CLIENT_UPDATE_KEY ...`).

### Автотест самообновления в CI (windows-latest)

Джоба `selfupdate-e2e-windows` в `.github/workflows/ci.yml` проверяет замену
работающего exe на настоящей Windows, чтобы не делать это руками при каждом
изменении `client_agent.py`. Она собирает две версии exe (текущую из
`client/VERSION` и следующую, файл потом возвращается через `git checkout`),
поднимает стаб сервера релизов на `127.0.0.1:8000` (адрес встроен в exe, см.
выше) и запускает старую exe с `--apply-update-now`. Сценарии в
`client/tests/test_selfupdate_e2e.py`:

- обновление: код выхода 0, на месте старой exe лежит новая (sha256), старая
  ушла в `.old`, новая версия перезапущена и сама сходила на сервер; повторный
  запуск — `up_to_date` без скачивания, `.old` удалён при старте;
- подмена: sha256 в метаданных не совпадает с байтами — код выхода 1, exe не
  заменён, `.download` удалён.

Джоба не зависит от `build-windows-agent`. Без переменных
`MONITORING_SELFUPDATE_E2E_*` тест пропускается (так он ведёт себя в джобе
`test`). Планировщик заданий (AtLogOn/сторож) в CI не проверяется: на раннере
нет интерактивной сессии входа, это остаётся ручной проверкой.

### Автозапуск через Планировщик заданий (Windows, без прав администратора)

Скрипты в `client/scheduler/` регистрируют две задачи от имени текущего пользователя:

- `MonitoringAgent-OnLogon` — запуск `client_agent.exe` при входе пользователя;
- `MonitoringAgent-Watchdog` — раз в 5 минут запускает агент, если процесс
  `client_agent` не работает (подстраховка; основной перезапуск после
  обновления делает сам агент).

Установка на машине (скрипты и `client_agent.exe` в одном каталоге, иначе
укажите `-AgentPath`):

```powershell
setx MONITORING_API_KEY "ключ_приёма"
setx MONITORING_CLIENT_UPDATE_KEY "ключ_обновлений"
powershell -NoProfile -ExecutionPolicy Bypass -File client\scheduler\install-tasks.ps1 -AgentPath "C:\monitoring\client_agent.exe"
Get-ScheduledTask -TaskName "MonitoringAgent-*" | Format-Table TaskName, State
```

Удаление задач: `powershell -NoProfile -ExecutionPolicy Bypass -File client\scheduler\uninstall-tasks.ps1`.
Переменные из `setx` попадают в новые процессы, поэтому после них перезапустите
агент (или дождитесь сторожа).

## CI/CD: автопубликация релиза агента

В `.github/workflows/ci.yml` джоба `publish-client-release` берёт
`client_agent.exe`, собранный джобой `build-windows-agent` в том же прогоне,
читает версию из `client/VERSION` и публикует релиз на сервер запросом
`POST /api/client-release`. Агенты подхватят его при следующей проверке.

Условия запуска (заданы в файле, не в настройках GitHub):

- только событие `push` в ветку `master` этого репозитория;
- только если в этом push менялось что-то в `client/**` (джоба
  `detect-client-changes`);
- после зелёных `test`, `build-windows-agent` и `selfupdate-e2e-windows`;
- никогда на `pull_request`; триггера `workflow_dispatch` у workflow нет.

Джоба выполняется на self-hosted раннере с меткой `monitoring-publisher`,
потому что сервер мониторинга сейчас доступен только с этой машины.

Секреты репозитория (Settings → Secrets and variables → Actions):

| Секрет | Значение |
|---|---|
| `MONITORING_SERVER_URL` | базовый адрес сервера, сейчас `http://localhost:8000`, для VPS — его адрес |
| `MONITORING_API_KEY` | `api_key` сервера (тот же, что у compose-стека) |

Поток релиза: поменять `client/VERSION` (и код агента) → PR → squash-merge в
`master` → CI собирает exe → публикация → агенты обновляются в течение
одного интервала опроса. Если `client/**` менялся, а `VERSION` — нет, на сервер
уйдёт новая сборка с той же версией: агенты с этой версией её не скачают
(обновление только на строго новую версию).

Self-hosted раннер в WSL2 (регистрируется вручную, токен выдаёт GitHub в
Settings → Actions → Runners → New self-hosted runner → Linux x64):

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
# ссылку на архив и одноразовый токен возьмите со страницы New self-hosted runner
curl -o actions-runner-linux-x64.tar.gz -L <url_из_GitHub>
tar xzf actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/ddyonth/monitoring-project --token <ТОКЕН> \
  --name wsl-publisher --labels monitoring-publisher --unattended
./run.sh                      # интерактивно, пока открыт терминал
# либо как systemd-сервис (WSL2 с systemd):
sudo ./svc.sh install $USER && sudo ./svc.sh start
```

Раннер должен видеть сервер по `MONITORING_SERVER_URL`: при `localhost:8000`
compose-стек в `/opt/monitoring` должен быть запущен на той же машине.

Отключить: `sudo ./svc.sh stop` (или закрыть `run.sh`); разрегистрировать
навсегда: `./config.sh remove --token <ТОКЕН_УДАЛЕНИЯ>` (токен со страницы
раннера в GitHub) и удалить каталог. Пока раннер выключен, джоба публикации
будет висеть в очереди до его появления, остальные джобы CI это не блокирует.

Обязательная настройка для публичного репозитория с self-hosted раннером:
Settings → Actions → General → Fork pull request workflows from outside
collaborators → **Require approval for all external contributors**. PR из форка
исполняет `ci.yml` из форка, и условие `if` в файле само по себе от такого PR
не защищает.

## Секреты

Ключи `api_key` (сервер и агент) и `client_update_key` (сервер) в файлах
`server/config.json` и `client/config.json` — это шаблоны с placeholder
`CHANGE_ME_LOCAL_KEY`, они закоммичены в репозиторий и нужны только для того,
чтобы приложение стартовало локально без настройки.

Для реального использования ключи задаются через переменные окружения, а не
правкой закоммиченного `config.json`:

| Переменная | Кто читает | Что переопределяет |
|---|---|---|
| `MONITORING_API_KEY` | сервер и агент | `api_key` |
| `MONITORING_CLIENT_UPDATE_KEY` | сервер | `client_update_key` |

Приоритет: если переменная задана и не пустая, берётся она; иначе значение из
`config.json`; если и там нет — встроенное значение по умолчанию из кода.
Пример имён переменных — в `.env.example` (файл `.env` в `.gitignore`).

Пример запуска сервера с ключом из окружения:

```powershell
$env:MONITORING_API_KEY = "секрет"
python server/server_app.py
```

```bash
MONITORING_API_KEY=секрет python server/server_app.py
```

## Запуск сервера в Docker

Образ описан в `server/Dockerfile` (python:3.11-slim), контекст сборки — корень
репозитория. `docker-compose.yml` поднимает два сервиса: `postgres` (PostgreSQL 18,
данные в named volume `postgres_data`) и `server` на порту 8000.

Сервер хранит данные в PostgreSQL (psycopg 3). Строка подключения берётся из
`MONITORING_DATABASE_URL` (в compose собирается автоматически из `POSTGRES_*`).
Секреты передаются из окружения хоста или из `.env` рядом с compose-файлом
(см. `.env.example`); без `MONITORING_API_KEY` и `POSTGRES_PASSWORD` compose
не стартует:

```bash
export MONITORING_API_KEY=секрет
export POSTGRES_PASSWORD=пароль_бд
docker compose up -d --build
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
```

База создаётся с локалью `C.UTF-8`: даты хранятся как TEXT в ISO-формате и
сравниваются как строки, порядок сравнения такой же, как раньше в SQLite.

## Тесты

Тесты `server/tests` гоняются против настоящего Postgres: conftest создаёт
отдельную базу `monitoring_test` в том же сервере (рабочие данные не трогает),
схему создаёт `ensure_schema()`, после каждого теста таблицы очищаются
(`TRUNCATE`). Адрес базы — `MONITORING_TEST_DATABASE_URL` или
`MONITORING_DATABASE_URL` с суффиксом `_test`.

Локально из venv (Postgres из compose слушает 127.0.0.1:5432):

```bash
pip install -r requirements.txt -r requirements-dev.txt
docker compose up -d postgres
export MONITORING_TEST_DATABASE_URL=postgresql://monitoring:пароль_бд@localhost:5432/monitoring_test
python -m pytest server/tests -v
```

Внутри контейнера, в том же окружении, что и сервер (стадия `test` в Dockerfile):

```bash
docker compose run --rm tests
```

В CI (`.github/workflows/ci.yml`) Postgres поднимается как service container.

## Деплой сервера на Linux (Ansible)

Плейбук в `deploy/ansible/` разворачивает сервер через Docker Compose в
`/opt/monitoring`: ставит Docker Engine и compose-plugin из официального
apt-репозитория Docker, копирует туда `docker-compose.yml`, `server/Dockerfile`,
`.dockerignore`, `requirements.txt` и код `server/` (те же файлы, что в
репозитории, без изменений), рендерит `server/config.json` без секретов и
`.env` с секретами (0600), после чего выполняет `docker compose up -d --build`.
Образ собирается на целевом хосте, registry не нужен.

Старый деплой этой же роли (venv + systemd-юнит `monitoring-server`) при первом
прогоне останавливается, юнит отключается и удаляется; каталог
`/opt/monitoring/venv` не трогается (на случай отката).

Control node — только Linux (например, WSL2 с Ubuntu); цель — любой
apt-based хост (Debian/Ubuntu). Сейчас в инвентаре `localhost`, для VPS см.
комментарий в `deploy/ansible/inventory/hosts.ini`.

1. Создать файл секретов из примера и зашифровать его (в репозиторий он не
   попадает, путь есть в `.gitignore`):

```bash
cp deploy/ansible/group_vars/local/vault.yml.example deploy/ansible/group_vars/local/vault.yml
# заполнить monitoring_api_key, monitoring_client_update_key, monitoring_postgres_password
ansible-vault encrypt deploy/ansible/group_vars/local/vault.yml
```

2. Запустить плейбук (нужен sudo на целевом хосте, поэтому `--ask-become-pass`):

```bash
ansible-playbook -i deploy/ansible/inventory/hosts.ini deploy/ansible/playbook.yml --ask-vault-pass --ask-become-pass
```

3. Проверить результат:

```bash
sudo docker compose -f /opt/monitoring/docker-compose.yml ps   # postgres healthy, server running
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
systemctl status monitoring-server                             # Unit ... could not be found
```

Повторный запуск плейбука без изменений в коде и переменных ничего не меняет:
`docker compose up -d --build` при попадании в кэш сборки контейнеры не
пересоздаёт. При изменении кода образ пересобирается и контейнер сервера
пересоздаётся той же командой, отдельный рестарт не нужен.

## Примечания

- Основной сценарий использования проекта — запуск клиентской и серверной частей в виде `.exe`.
- Серверное приложение удобно собирать и запускать с консольным окном, чтобы видеть логи и состояние запуска.
- Клиентское приложение лучше без отдельного окна: после старта оно работает в фоне и периодически отправляет данные на сервер.
- Запуск из `.py` нужен в первую очередь для разработки, отладки и ручной проверки.
