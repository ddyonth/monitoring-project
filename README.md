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

Плейбук в `deploy/ansible/` разворачивает сервер как systemd-сервис
`monitoring-server` в `/opt/monitoring` (venv, код `server/`, `config.json`
без секретов, unit-файл с `WorkingDirectory` и `EnvironmentFile=` на отдельный env-файл 0600 с `MONITORING_API_KEY`).
Control node — только Linux (например, WSL2 с Ubuntu); цель — любой
apt-based хост (Debian/Ubuntu). Сейчас в инвентаре `localhost`, для VPS см.
комментарий в `deploy/ansible/inventory/hosts.ini`.

1. Создать файл секретов из примера и зашифровать его (в репозиторий он не
   попадает, путь есть в `.gitignore`):

```bash
cp deploy/ansible/group_vars/local/vault.yml.example deploy/ansible/group_vars/local/vault.yml
# отредактировать значения monitoring_api_key / monitoring_client_update_key
ansible-vault encrypt deploy/ansible/group_vars/local/vault.yml
```

2. Запустить плейбук (нужен sudo на целевом хосте, поэтому `--ask-become-pass`):

```bash
ansible-playbook -i deploy/ansible/inventory/hosts.ini deploy/ansible/playbook.yml --ask-vault-pass --ask-become-pass
```

3. Проверить, что сервис жив (эндпоинта `/health` нет, дашборд отдаётся без ключа):

```bash
systemctl status monitoring-server
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
```

Повторный запуск плейбука без изменений в коде и переменных не должен
ничего менять (идемпотентность); при изменении кода/конфига сервис
перезапускается handler-ом.

## Примечания

- Основной сценарий использования проекта — запуск клиентской и серверной частей в виде `.exe`.
- Серверное приложение удобно собирать и запускать с консольным окном, чтобы видеть логи и состояние запуска.
- Клиентское приложение лучше без отдельного окна: после старта оно работает в фоне и периодически отправляет данные на сервер.
- Запуск из `.py` нужен в первую очередь для разработки, отладки и ручной проверки.
