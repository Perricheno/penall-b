# BUILD_PLAN — PenAll Backend

Полный план работ для агента. **Режим работы:** одна задача (или связка мелких) = одна сессия. В начале сессии: прочитать этот файл, найти первую незакрытую задачу, загрузить указанный контекст, сделать, прогнать DoD из CLAUDE.md, проставить чекбокс, закоммитить.

Вехи M0–M9 — строго по порядку (каждая опирается на предыдущую). M10–M11 можно вести слоями параллельно после M3.

---

## M0 — Каркас (цель: `make dev` работает, CI зелёный)

- [ ] **0.1** Скелет `api/`: FastAPI-приложение, `core/config.py` на pydantic-settings (ENV: `APP_ENV`, `DATABASE_URL`, `REDIS_URL`, `S3_ENDPOINT/KEY/SECRET/BUCKET`, `JWT_SECRET`, `OTP_SECRET`, `PAY_KASPI_TRADEPOINT_ID`, `PAY_KASPI_API_KEY`, `KASPI_BASE_URL`, `FCM_CREDENTIALS_JSON`, `SENTRY_DSN`), роут `GET /health` (проверяет pg+redis).
- [ ] **0.2** `docker-compose.dev.yml`: postgres:16, redis:7, minio + init-бакет `media`; `Dockerfile` (slim, non-root); `Makefile` с командами из CLAUDE.md.
- [ ] **0.3** Alembic в async-режиме, пустая базовая ревизия, `make migrate/upgrade`.
- [ ] **0.4** Глобальный обработчик ошибок → формат `{"error":{...}}` (CLAUDE.md п.12); structlog JSON + request_id middleware.
- [ ] **0.5** `.gitlab-ci.yml`: stages lint → test (services: pg, redis) → build (kaniko) → deploy-staging (пока заглушка `echo`, включим в M12).

## M1 — Схема БД (контекст: `api_logic §1` + правки `feature_guides §17`)

- [ ] **1.1** ORM-модели: users(+is_blocked), refresh_tokens, devices, categories, products(+weight_g, stock_source, attr_order, tsv generated), banners, favorites, orders, order_items, payments(+transaction_id), promo_codes, promo_usages, bonus_accounts(CHECK balance>=0), bonus_transactions(+spent_amount, status), bonus_tiers, settings(key/value JSONB), import_logs, idempotency_keys, search_queries. Таблицы `otp_codes` НЕТ (Redis).
- [ ] **1.2** Миграция №1: расширения `pg_trgm`, `unaccent`; индексы: products(category_id,is_active), GIN(tsv), GIN(name gin_trgm_ops), GIN(sku gin_trgm_ops), orders(user_id, created_at), уникальности по контракту.
- [ ] **1.3** `scripts/seed.py` (идемпотентный): 3 корневых / 10 подкатегорий / 60 товаров (реалистичные канцтовары, цены 90–15000 ₸, часть с old_price, часть stock=0, характеристики Бренд/Цвет), тиры бонусов 0/1%/3%, дефолтные settings, admin-пользователь.
- [ ] **1.4** `scripts/create_admin.py` (email+пароль → users role=admin).

## M2 — Auth (контекст: `feature_guides блок 1`, `api_logic §4.1`)

- [ ] **2.1** `POST /auth/otp/request`: phonenumbers-валидация, Redis-лимиты (3/час/номер, 10/час/IP), HMAC-код в `otp:{phone}` TTL 300, `OtpChannel` интерфейс + `SmsMobizonChannel` (httpx; в dev — канал `console`, печатает код в лог DEV-режима) + заглушка `WhatsAppChannel` за флагом.
- [ ] **2.2** `POST /auth/otp/verify`: атомарный HINCRBY attempts, ≤5 попыток, constant-time сравнение, создание user + bonus_account, выдача пары (PyJWT access 30 мин / opaque refresh 30 дней в refresh_tokens).
- [ ] **2.3** `POST /auth/refresh` с ротацией и отзывом; deps `get_current_user`, `get_optional_user`, `require_admin`.
- [ ] **2.4** Тест-номер ревью сторов из settings (`review_phone`/`review_code`) — SMS не шлётся.
- [ ] **2.5** `GET/PUT/DELETE /me` (delete = soft + анонимизация + отзыв токенов + чистка devices).
- [ ] **2.6** Тесты: лимиты, попытки, ротация, protected-роут без/с токеном, тест-номер.

## M3 — Каталог на чтение (контекст: `api_logic §5, §7`, `feature_guides 2–4`)

- [ ] **3.1** Кэш-слой: `catalog:ver` (INCR-инвалидация), хелперы get_or_set.
- [ ] **3.2** `GET /categories` (дерево, кэш), `GET /config` (сборка из settings + bonus_tiers, кэш 5 мин).
- [ ] **3.3** `GET /products`: фильтры/сортировки/attrs `@>`/пагинация, stock=0 в конец, bonus_amount по уровню запросившего, is_favorite одним JOIN.
- [ ] **3.4** Поиск внутри 3.3 по SQL из `feature_guides блок 3` + `GET /search/suggest` + upsert `search_queries`.
- [ ] **3.5** `GET /products/{id}` (+similar по правилу), `GET /products/facets`, `GET /banners`, `GET /collections/{key}`.
- [ ] **3.6** Тесты на сидах: морфология («ручки синие»), опечатка («тетрад»), SKU-префикс, фасеты, инвалидация ver после апдейта товара.

## M4 — Медиа (контекст: `feature_guides блок 10`)

- [ ] **4.1** MinIO-клиент, `POST /admin/uploads`: валидация, Pillow-пайплайн (exif_transpose → RGB → 200/500/1200 LANCZOS → WebP q82), ключи `p/{uuid}_{size}.webp`, ответ thumb/card/full.
- [ ] **4.2** Отдача: в dev — прокси-роут/публичный minio; в prod — ingress `cdn.` (манифесты в M12). URL-константа из ENV `MEDIA_BASE_URL`.

## M5 — Импорт/экспорт XLSX (контекст: `feature_guides блок 11`, `api_logic §4.7`)

- [ ] **5.1** `POST /admin/products/import?dry_run=`: openpyxl read_only, two-pass (валидация всех → отчёт; запись чанками 500, upsert по SKU, images/manual-остатки не трогаем), import_logs.
- [ ] **5.2** Пресет колонок «uMag» (селектор формата) — маппинг их экспорта на наш шаблон.
- [ ] **5.3** `GET /admin/products/export` (write_only, тот же шаблон), `GET /admin/imports`.
- [ ] **5.4** `POST /admin/products/bulk` (price_percent ≥1, stock_set только для stock_source=manual — иначе синк перетрёт, activate/deactivate) + bump ver.
- [ ] **5.5** Тест производительности: сгенерировать файл 4000 строк, импорт < 2 мин, повторный импорт = 0 created.
- [ ] **5.6** Накладная `GET /admin/orders/{id}/invoice` — адаптация существующего openpyxl-пайплайна З-2. **Блокер: шаблон З-2 приложит владелец** (см. «Входящие»).

## M6 — Корзина и промокоды (контекст: `api_logic §2, §5`)

- [ ] **6.1** `services/cart.py::calculate_cart()` — канон §2 целиком (промокод внутри, bonus_max, доставка pickup/courier; kazpost — заглушка тарифа до M13). **Тест `test_calculate_canonical` — точные числа из §2.**
- [ ] **6.2** `POST /cart/calculate`: qty-урезание, unavailable, price_changed, promo.error_code (200, не 4xx).
- [ ] **6.3** Промокоды: проверка всех лимитов; тесты: expired, min_order, per_user_limit, stacks_with_bonus=false.

## M7 — Заказы (контекст: `api_logic §3.1, §4.2, §4.4`)

- [ ] **7.1** `POST /orders`: Idempotency-Key, пересчёт, client_total → 409 PRICE_CHANGED с полным calculate в details, транзакция: FOR UPDATE по возрастанию id → повторная проверка → списание stock → orders+items-снапшоты → FIFO-redeem бонусов → promo_usage → idempotency_keys. Номер `ORD-{год}-{seq:06d}`.
- [ ] **7.2** `GET /orders`, `GET /orders/{id}` (только свой), `POST /orders/{id}/cancel` (только new; полный возврат).
- [ ] **7.3** Статусная машина: словарь переходов + side effects (§3.1), `PATCH /admin/orders/{id}/status` (+track_number для kazpost), `POST /admin/orders/{id}/cancel`.
- [ ] **7.4** Arq: автоотмена неоплаченных (каждые 5 мин, FOR UPDATE-гонка по §4.4).
- [ ] **7.5** Тесты: идемпотентность создания, гонка остатка (2 конкурентных заказа на последний товар), вся таблица переходов, автоотмена возвращает stock и бонусы.

## M8 — Оплата Kaspi + чек (контекст: `kaspi_pay_integration.md` ЦЕЛИКОМ)

- [ ] **8.1** `adapters/kaspi.py`: подпись (эталонный тест-вектор), create_payment (orderId `{number}-{attempt}`), get_status, parse_webhook, refund. База URL из ENV.
- [ ] **8.2** `POST /orders/{id}/pay` (повторные попытки → новая строка payments, продление expires), страницы `/payment/return|fail` (простой HTML «Вернитесь в приложение»).
- [ ] **8.3** `POST /payments/webhook/kaspi`: подпись → идемпотентность → сверка суммы → единая `apply_payment_status()` → 200; фоново: чек + push + алерт-хук.
- [ ] **8.4** Резервный поллинг Arq (1 мин, потом каждые 2 до expires).
- [ ] **8.5** Refund в admin-cancel оплаченного; чек возврата.
- [ ] **8.6** `adapters/greenkassa.py`: интерфейс FiscalProvider + мок-реализация; боевые вызовы — **блокер: дока GreenKassa от владельца**. Ретраи ×5 backoff, receipt_url в заказ.
- [ ] **8.7** Тесты = чек-лист раздела 12 kaspi-ТЗ (моками): потерянный вебхук, дубль, неверная подпись/сумма, оплата после автоотмены.

## M9 — Бонусы и push (контекст: `api_logic §4.5–4.6, §8`, `feature_guides 8–9`)

- [ ] **9.1** Начисление при done (+lifetime_spent), revert при отменах, FIFO-redeem уже из M7 — довести лоты (spent_amount/status).
- [ ] **9.2** Кроны: expire (03:00, FIFO-остатки), напоминание за 7 дней (10:00).
- [ ] **9.3** `GET /me/bonuses` (tier/next_tier/remaining/expiring_soon), `/me/bonuses/transactions`, admin bonus-adjust (комментарий обязателен), `PUT/GET /admin/bonus-tiers`.
- [ ] **9.4** `POST /devices`; `services/notifications.py` — все тексты из §8 в одном месте; firebase-admin отправка из Arq; чистка UNREGISTERED. Хук во все переходы статусов.
- [ ] **9.5** Тесты: канон начисления, сгорание частично потраченного лота, adjust, смена тира после done.

## M10 — Остальной admin-API

- [ ] **10.1** products CRUD (delete=скрытие; ручная правка stock → stock_source=manual), categories CRUD + reorder (409 CATEGORY_NOT_EMPTY).
- [ ] **10.2** orders list с фильтрами/поиском, users list/detail/block, promo CRUD, banners CRUD, `GET/PUT /admin/settings`, `GET /admin/dashboard` (today/week/по статусам/топ-10).
- [ ] **10.3** Фавориты/мелочь мобильного контракта, если что-то осталось непокрытым — сверить весь `openapi.yaml` по чек-листу путей.

## M11 — Админка React (контекст: `feature_guides блок 13`; после M3, слоями)

- [ ] **11.1** Bootstrap: Vite+TS+shadcn, openapi-typescript из `contracts/`, api-клиент с refresh, логин, layout с меню.
- [ ] **11.2** Товары: таблица (поиск/фильтры), форма (характеристики динамическими строками, фото drag-sort), **импорт** (dropzone → отчёт dry-run с фильтром «только ошибки» → применить), экспорт, bulk.
- [ ] **11.3** Заказы: список, деталка, статусы с подтверждением (+track для kazpost), отмена, накладная.
- [ ] **11.4** Категории (дерево dnd), баннеры.
- [ ] **11.5** Промокоды, бонус-тиры, настройки, пользователи (+adjust бонусов), дашборд.

## M12 — Прод-готовность (контекст: `feature_guides блоки 15–16`)

- [ ] **12.1** `deploy/`: kustomize base+overlays (api, arq-worker, admin, minio, ingress+cert-manager, cdn-хост), `SECRETS.md` со списком ключей.
- [ ] **12.2** CI: автодеплой staging из main; тег `v*` → prod (manual gate). Prod-контекст — PS.kz.
- [ ] **12.3** CronJob pg_dump → S3 (30 дней) + **репетиция restore на staging**.
- [ ] **12.4** sentry-sdk, Telegram-алерт (5xx/вебхук-фейлы), healthcheck TradePointId на старте.
- [ ] **12.5** Нагрузочный прогон: k6/hey на листинг+поиск, цель p95 < 300 мс на staging-данных 4000 SKU.
- [ ] **12.6** Полный прогон чек-листа приёмки `tz §13` (технические пункты 12–15 — здесь).

## M13 — uMag-синк (блокирован до API-доков; контекст: `api_logic §10.4`)

- [ ] **13.1** `adapters/umag.py` по их докам → `catalog_sync.upsert`, cron 10 мин, кнопка «Синхронизировать сейчас», формула резервов, только stock_source=umag.

---

## Входящие от владельца (блокеры — спрашивать, не выдумывать)

| Что | Блокирует |
|---|---|
| Официальная дока Kaspi из кабинета (сверка полей) → `docs/kaspi/` | финал M8 |
| Дока API GreenKassa + регистрация кассы | 8.6 |
| Доступ/доки API uMag | M13 (не релиз) |
| Шаблон накладной З-2 (xlsx) | 5.6 |
| Домен (сейчас плейсхолдер SHOP.kz) и хост PS.kz | M12 |
| Реквизиты магазина, адрес самовывоза, номер WhatsApp, оферта/политика | settings в M12 |
| XLSX-экспорт из uMag (реальный файл) | пресет 5.2 |
