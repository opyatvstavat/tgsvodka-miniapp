# Деплой на Railway

Проект — **один сервис** `web`: Mini App, API, Telethon ingest и база SQLite на примонтированном volume.

Так было не всегда. Раньше сервисов было три — web, worker и Postgres — и все они платные круглосуточно, потому что Railway считает деньги за занятую память, а не за нагрузку. Ingest переехал фоновой задачей внутрь web, база — в SQLite на volume. Осталась одна память одного процесса.

## 1. Репозиторий

Код: https://github.com/OcherednRra/tgsvodka-miniapp

## 2. Создать проект

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → `OcherednRra/tgsvodka-miniapp`.
2. **Settings** → **Build**:
   - Builder: **Dockerfile**
   - Dockerfile path: `Dockerfile.web`
3. **Settings** → **Networking** → **Generate Domain** (HTTPS). Домен станет `WEBAPP_URL`.
4. **Settings** → **Volumes** → примонтировать volume на **`/data`**.

**Replicas = 1.** Масштабировать нельзя по двум причинам: две реплики — это два Telethon-клиента, которые начнут дублировать форварды в канал сводки, и два писателя в один файл SQLite.

## 3. Переменные

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен от @BotFather |
| `WEBAPP_URL` | HTTPS-домен сервиса (без `/` в конце) |
| `ALLOWED_USER_ID` | Необязательно: Telegram user id, если ленту нужно закрыть от остальных |
| `OWNER_USER_ID` | Ваш Telegram user id: комментарии, highlights и `/stats` только у владельца. По умолчанию берётся из `ALLOWED_USER_ID` |
| `SUMMARY_CHAT_ID` | `-100…` канала сводки `@tgsvodka` |
| `HIGHLIGHTS_CHAT_ID` | `-100…` канала хайлайтов |
| `TARGET_CHANNEL` | `@tgsvodka` |
| `HIGHLIGHTS_CHANNEL` | `@tgsvodka_highlights` |
| `OWN_CHANNELS` | Ваши каналы через запятую |
| `API_ID` | my.telegram.org |
| `API_HASH` | my.telegram.org |
| `WEB_SESSION_STRING` | Сессия Telethon (и медиа, и ingest) |
| `SQLITE_PATH` | `/data/tgsvodka.db` — по умолчанию именно это |
| `MEDIA_CACHE_DIR` | `/data/media` |
| `MEDIA_CACHE_MAX_MB` | Потолок кэша медиа, по умолчанию `1500`; в проде стоит `200` |
| `BACKFILL_LIMIT` | Сколько постов сканирует backfill, по умолчанию `1000` |
| `BACKFILL_INTERVAL` | Период backfill в секундах, по умолчанию `3600` |
| `FORCE_UPDATE_INTERVAL` | Период keepalive Telethon в секундах, по умолчанию `300` |

`DATABASE_URL` **не задавайте** — пустая переменная означает SQLite по пути `SQLITE_PATH`. Если всё же нужен Postgres, положите туда `postgresql://…`, и код переключится сам.

Сессия ищется в порядке `SESSION_STRING` → `WORKER_SESSION_STRING` → `WEB_SESSION_STRING`, достаточно любой одной.

## 4. Volume

На volume лежат две вещи: база `/data/tgsvodka.db` и кэш медиа `/data/media`.

Кэш вытесняет самые давно не использованные файлы, держась под `MEDIA_CACHE_MAX_MB`. Удаляются только файлы вида `<chat_id>_<msg_id>.<ext>` — база и её WAL под вытеснение не попадают, даже лежа в соседнем каталоге.

Без volume оба потеряются при первом же деплое.

**Потолок кэша влияет на счёт, а не только на диск.** Прочитанные файлы оседают в page cache, а Railway считает память контейнера вместе с ним: чем больше кэш успевает набрать, тем выше память, за которую вы платите. Поэтому в проде стоит `200`, а не дефолтные `1500`.

При старте база приводится в порядок сама: одноразовое пересжатие `media_json` (отмечается в `PRAGMA user_version` и повторно не запускается) и обрезка WAL — SQLite переиспользует его файл, но никогда не уменьшает, так что одна большая транзакция иначе оставляет его раздутым навсегда.

## 5. Telegram

1. Бот — **админ** в канале сводки и хайлайтов (права на постинг).
2. @BotFather → бот → **Bot Settings** → **Menu Button** / **Mini App**: URL = `WEBAPP_URL`.
3. Без `ALLOWED_USER_ID` ленту открывает любой. У каждого свои каналы и свои просмотренные; комментировать от аккаунта Telethon может только `OWNER_USER_ID`.

### Сессия Telethon

```bash
pip install -r requirements.txt
# .env: API_ID, API_HASH
python scripts/export_session.py --session-name tgsvodka_web
# → в Railway: WEB_SESSION_STRING=...
```

Одну строку сессии **нельзя** использовать с двух IP одновременно — например, Railway и локальный Docker вместе. Telegram ответит `AuthKeyDuplicatedError`. Для локальной работы заведите вторую сессию либо остановите Railway.

## 6. Проверка

- `https://<домен>/health` → `{"status":"ok"}`.
- Логи: `Telethon ingest running in-process`, `Authorized as …`, затем `Backfill complete` и `Saved live post`.
- Бот в Telegram → Mini App → лента.

## 7. Обновление

```bash
git push origin main
```

Если auto-deploy выключен — кнопка **Deploy** в Railway или из CLI:

```bash
railway api 'mutation { serviceInstanceDeploy(serviceId: "<service id>", environmentId: "<env id>", latestCommit: true) }'
```

## Переезд с Postgres на SQLite

Если у вас ещё живёт старый Postgres-сервис:

```bash
# 1) Забрать данные в файл (можно на работающей базе — скрипт досверяет
#    по первичным ключам всё, что доехало во время копирования)
SOURCE_DATABASE_URL="$(railway variables --service Postgres --kv | grep '^DATABASE_PUBLIC_URL=' | cut -d= -f2-)" \
  python scripts/migrate_to_sqlite.py tgsvodka.db

# 2) Положить на volume
railway volume files --volume web-volume upload tgsvodka.db /tgsvodka.db --overwrite

# 3) Убрать DATABASE_URL, задать пути, задеплоить, проверить ленту
# 4) Только после проверки удалить сервис Postgres
```

Порядок важен: удалять Postgres нужно последним, когда лента на SQLite уже работает.

## Частые проблемы

| Симптом | Решение |
|---|---|
| Mini App не открывается | `WEBAPP_URL` = точный HTTPS домен в BotFather |
| `AuthKeyDuplicatedError` | Одна сессия с двух IP (Railway + локальный Docker) → заведите вторую |
| Нет медиа | `API_ID`, `API_HASH`, `WEB_SESSION_STRING` |
| Пустая лента | В логах есть `Telethon ingest running in-process`? Сессия валидна? |
| Лента пуста после деплоя | Volume не примонтирован на `/data` — база не найдена, создалась пустая |
| Дубли постов в сводке | Replicas > 1 либо где-то ещё запущен ingest на той же сессии |
| Медиа качается медленно после деплоя | Volume не примонтирован — кэш пропал вместе с контейнером |
| Like не работает | `HIGHLIGHTS_CHANNEL`, `HIGHLIGHTS_CHAT_ID`, бот — админ хайлайтов |

## Расходы

Счёт Railway — это почти целиком память, а не CPU: простаивающий сервис всё равно платный. Что дало эффект:

- один сервис вместо трёх (ingest внутри web, база в SQLite);
- volume, чтобы кэш медиа и база переживали деплой;
- `MEDIA_CACHE_MAX_MB` — потолок на диск под кэш;
- префетч в ленте греет только превью, а не полные видео.

Разбивка по сервисам за период:

```bash
railway api 'query { usage(projectId: "<project id>",
  measurements: [CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB],
  groupBy: [SERVICE_ID], startDate: "…", endDate: "…")
  { measurement value tags { serviceId } } }'
```

API возвращает **несколько строк на сервис** — значения нужно суммировать, а не брать последнее.

## Локально (Docker)

```bash
cp .env.example .env
docker compose up --build
```

Локально поднимается ровно то же, что в проде: один контейнер с SQLite. Именно расхождение сред раньше прятало проблему — на Postgres с сотнями мегабайт кэша в памяти медленные запросы были незаметны, а на SQLite тот же запрос занимал секунды.

Ngrok для теста Mini App локально: `WEBAPP_URL=https://….ngrok-free.dev`
