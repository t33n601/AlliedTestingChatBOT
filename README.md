# KB Helper — локальный RAG-бот

Проект по курсу **AT AI Assistant** (31 шаг): Postgres + pgvector, эмбеддинги Ollama, API на FastAPI, чат на Gradio, тесты pytest и E2E-тест Selenium на Selenoid.
Все имена отличаются от курсовых, чтобы не конфликтовать со старым проектом (`~/Documents/AIproject`).

## Что как называется

| В презентации | В этом проекте |
|---|---|
| контейнер `at_postgres`, порт 5433 | `kb_vectordb`, порт **5435** |
| БД `at_data_db`, юзер `vlada` / `abc` | `knowledge_db`, `kbuser` / `kbpass` |
| таблица `at_data` (page, title, context, *_vector) | `kb_chunks` (source, heading, body, *_vec) |
| `files/` | `knowledge/` |
| `embedding.py` · `calcEmbed()` · `updateVector()` | `vectorize.py` · `make_vector()` · `fill_vectors()` |
| `localdb.py` · `insertInfo()` · `loadFiles()` | `loader.py` · `save_chunk()` · `import_folder()` |
| `atassistantapi.py` · `PageInfo` · `getDbCon()`, порт 8080 | `ragserver.py` · `ChunkInfo` · `get_conn()`, порт **8091** |
| эндпоинты `/at_data/...` | `/kb/...` |
| `gradio_app.py` · `ask_bot()`, порт 7860 | `chat_ui.py` · `reply()`, порт **7871** |
| `attest.py` | `test_ragserver.py` |
| `tests/seleniumtest.py::test_open_gradio_app` | `tests/test_ui_selenium.py::test_open_chat_ui` |
| Selenoid :4444 / дашборд :8082 | `kb_selenoid` :4445 / `kb_selenoid_ui` :8083 |
| `venv/`, `postgres/` | `.venv/`, `pgdata/` |

## Первый запуск (один раз)

```bash
cd ~/Documents/secondAIproject
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

docker compose up -d kb_vectordb      # схема (pgvector + таблица) создаётся сама из schema.sql
python loader.py                      # 149 чанков в таблицу (НЕ запускать второй раз — будут дубли)
python vectorize.py                   # эмбеддинги для всех строк

docker exec kb_vectordb psql -U kbuser -d knowledge_db \
  -c "SELECT count(*), count(body_vec) FROM kb_chunks;"   # 149 | 149
```

## Каждый день — порядок важен

```bash
docker start kb_vectordb                         # 1 — база
# 2 — Ollama уже работает в фоне (nomic-embed-text, qwen3:1.7b)
uvicorn ragserver:app --port 8091 --reload       # 3 — терминал A → http://localhost:8091/docs
python chat_ui.py                                # 4 — терминал B → http://localhost:7871
```

## Новый интерфейс (v2) — http://localhost:8091

Тот же `uvicorn ragserver:app --port 8091` теперь отдаёт и сайт (`web/`), Gradio больше не обязателен.

- **Ответ печатается по словам** — `POST /kb/chat/stream` (Server-Sent Events: `rewrite → sources → think* → token* → done`).
- **Ссылки на источники** — модель ставит `[1]`, `[2]`, в чате это кликабельные чипы; под ответом список чанков с % похожести.
- **Reasoning** — размышления qwen3 (`think: true` в Ollama API) видны вживую и сворачиваются, когда начинается ответ.
- **Русский язык** — вопрос и история переписываются в английский поисковый запрос (`rewrite_query`), ответ на языке вопроса. Заодно работают уточняющие вопросы («а сколько это стоит?»).
- **Карта знаний** — все чанки на 2D-карте (t-SNE на numpy, `GET /kb/map`), вопрос появляется на карте с лучами к найденным чанкам (`GET /kb/retrieve`).
- **Документы** — загрузка PDF/DOCX/MD/TXT (`POST /kb/upload`, разбиение в `chunking.py`), просмотр и удаление (`/kb/documents`).
- **Пайплайн** Retrieve → Augment → Generate подсвечивается по шагам с замером времени; 👍/👎 к ответам (`POST /kb/feedback/{id}`); каждый вопрос пишется в таблицу `kb_queries`.
- **Настройки (кнопка Tune)** — упражнения из курса прямо в интерфейсе: тон ответа (formal / friendly / brief / simple), сколько чанков искать (1–10), выбор модели из установленных в Ollama (`GET /kb/models`).
- **Редизайн v2** (макеты: `design/screens/redesign/`): рабочее место из трёх колонок (Runs + Tune · чат с пайплайном как тест-прогон · Evidence с мини-картой), карта с зумом/панорамой и подписями кластеров, документы таблицей с фильтром/сортировкой и инспектором (`POST /kb/documents/{source}/reembed`), мобильная версия с нижними вкладками и шторкой источников.
- **HNSW-индекс** на `body_vec` создаётся при старте.
- **Защита записи** — если задан `KB_ADMIN_TOKEN`, все POST/PUT/DELETE требуют заголовок `X-Admin-Token` (локально переменная не задана — всё открыто, тесты работают).

Настройки через env: `KB_DB_DSN`, `OLLAMA_HOST`, `KB_CHAT_MODEL`, `KB_EMBED_MODEL`, `KB_ADMIN_TOKEN`.

## Тесты

```bash
pytest test_ragserver.py -v                      # 19 интеграционных тестов API (вкл. стрим, карту, загрузку, re-embed, настройки)

./selenoid/start-selenoid.sh                     # браузеры в Docker, дашборд http://localhost:8083
pytest tests/test_ui_selenium.py -v              # E2E: открывает чат и задаёт вопрос
```

GitHub Actions на self-hosted runner: `runner/docker-compose.yaml` (нужны `KB_REPO_URL` и `KB_RUNNER_TOKEN`), workflow — `.github/workflows/ui-e2e.yml`.

pgAdmin по желанию: `docker compose --profile gui up -d` → http://localhost:5051 (`admin@kb.local` / `kbadmin`), сервер `kb_vectordb`, порт 5432.

## Дизайн чата

Стиль взят из логотипа Allied Testing: графит `#4B4B4D`, красный `#C14031`, серо-стальные шестерёнки `#979688`. Прямые углы, пиксельный шрифт Silkscreen для заголовков, IBM Plex Sans/Mono для текста (Google Fonts).

- `ui/logo.png`: логотип (вставляется в страницу как data URI)
- `ui/style.css`: вся тема (цвета заданы переменными `--kb-*` в начале файла)
- `ui/bot.svg`: аватар бота (графит + красный блок с битами)
- `chat_ui.py`: разметка (hero, карточка чата, примеры вопросов). Красная панель в плоском стиле логотипа рисуется в SVG (`motif_svg()`): жирные белые биты, сцепленные шестерёнки с тёмным торцом и белой обводкой (`gear_train()` сам считает расстановку и фазы) и молоток с объёмным бойком, который постукивает по шестерёнке, выбивая пиксельные искры. Анимации в `ui/style.css`, при «уменьшить движение» в системе они выключаются. Цифры в hero и статус «online» берутся из `GET /kb/stats` при каждой загрузке страницы.

## Отличия от кода на слайдах (исправлены баги)

- `/kb/search` и `/kb/answer` объявлены **до** `/kb/{chunk_id}`, иначе FastAPI пытается прочитать «search» как id и отдаёт 422.
- `conn`/`cur` инициализируются как `None`, чтобы `finally` не падал, если подключение к БД не удалось.
- В GET-by-id/PUT/DELETE 404 больше не превращается в 500.
- Векторные поля в `ChunkInfo` необязательные (API всё равно считает их сам).
- `pydantic==2.13.4` со слайда несовместим с `gradio==5.30.0` → в requirements `pydantic>=2.0`.
- Selenoid с новым Docker требует `DOCKER_API_VERSION=1.44` (уже в скрипте).

## Траблшутинг

- **address already in use** — порт занят: остановите старый сервер или смените порт.
- **No such container** — сначала `docker compose up -d kb_vectordb`, потом `docker start kb_vectordb`.
- **Error connecting to API** в чате — не запущен `ragserver` (шаг 3).
- **No similar context found** — таблица пустая или нет векторов: `python vectorize.py`.

## Лицензия

© 2026 t33n601. Все права защищены. Использование, копирование и изменение без письменного разрешения автора запрещены — см. [LICENSE](LICENSE).
