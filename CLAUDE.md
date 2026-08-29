# CLAUDE.md — PenAll Backend (penall-b)

## Что это

Бэкенд-монорепо интернет-магазина канцтоваров **PenAll** (Астана, РК, ассортимент 4000+ SKU): REST API (FastAPI), веб-админка (React), деплой (k3s). Мобильное приложение (Flutter) живёт в другом репозитории — его делает второй разработчик **по тому же контракту** `contracts/openapi.yaml`, поэтому контракт священен.

## Карта документов — что загружать в контекст

| Файл | Когда |
|---|---|
| `contracts/openapi.yaml` | ВСЕГДА при работе над API. Это закон. |
| `docs/BUILD_PLAN.md` | В начале каждой сессии: текущая задача, порядок, DoD |
| `docs/api_logic_v1.md` | Серверная логика: деньги (§2), статусы (§3), флоу (§4), по-эндпоинтно (§5), ошибки (§6), кэш (§7), пуши (§8), интеграции (§10) |
| `docs/feature_guides_v1.md` | Выбор библиотек и приёмы по каждому блоку |
| `docs/kaspi_pay_integration.md` | Всё про оплату Kaspi |
| `docs/tz_mobile_app_v1.md` | Продуктовый контекст и чек-лист приёмки (§13) |

## Стек (зафиксирован, не менять)

**API:** Python 3.12 · FastAPI · SQLAlchemy 2 async + asyncpg · Alembic · PostgreSQL 16 · Redis 7 · MinIO · Arq (фон/кроны) · PyJWT · httpx · openpyxl · Pillow · phonenumbers · firebase-admin · structlog · pytest + pytest-asyncio · ruff
**Админка:** Vite + React 18 + TypeScript · shadcn/ui · TanStack Query + Table · react-hook-form + zod · react-router v7 · openapi-typescript (типы из контракта)

## Целевая структура

```
api/app/{main.py, core/, models/, schemas/, routers/, services/, adapters/, tasks/}
api/tests/            api/alembic/
admin/                # React-админка
contracts/openapi.yaml
deploy/{base, overlays/staging, overlays/prod}
scripts/              docs/
Makefile              docker-compose.dev.yml
```

Роутеры тонкие — бизнес-логика в `services/`, внешние системы в `adapters/` (PaymentProvider, FiscalProvider, OtpChannel, DeliveryProvider, UmagConnector).

## Команды

```
make dev        # docker compose (pg16 + redis7 + minio) + uvicorn --reload
make test       # pytest
make lint       # ruff check + format --check
make migrate m="описание"   # alembic revision --autogenerate
make upgrade    # alembic upgrade head
make seed       # тестовые данные
```

## Железные правила

1. **Контракт — закон.** Не добавляй и не меняй эндпоинты/поля/enum «по ходу задачи». Нужно изменение — остановись, предложи диф `contracts/openapi.yaml` отдельным коммитом и жди явного подтверждения владельца.
2. **Деньги — int тенге.** Все суммы считает только `services/cart.py::calculate_cart()` по канону `api_logic §2`, округление floor. Тест канонического примера (§2) обязан существовать и проходить всегда.
3. Ценам, остаткам и суммам из запросов клиента **не верить никогда** — только БД.
4. Баланс бонусов меняется **только** в одной транзакции с записью в `bonus_transactions`; на `bonus_accounts.balance` стоит `CHECK (balance >= 0)`.
5. `payment_status=paid` ставится только обработчиком вебхука или сверкой статуса у Kaspi, с проверкой `amount == order.total`. Обработка идемпотентна по `payments.external_id`.
6. Остатки: `SELECT ... FOR UPDATE` в фиксированном порядке id; резерв при создании заказа, возврат при любой отмене — в одной транзакции.
7. OTP — только Redis с TTL (таблицы нет). Код никогда не попадает в логи. Телефоны — строго E.164.
8. Схема БД меняется только миграциями Alembic.
9. Секреты — только из ENV/k8s Secrets. В коде, тестах и compose — фейковые значения.
10. **Не добавляй «на будущее»:** никакого cod, Halyk, Forte, Meilisearch, Celery, Kafka. Если кажется, что нужно — спроси.
11. Логи — structlog JSON с request_id; без телефонов, токенов, кодов.
12. Формат ошибок — строго `{"error": {"code", "message", "details"}}`, коды из реестра `api_logic §6`; message — человекочитаемый русский.
13. Не выдумывай данные и реквизиты (домен, ключи, адрес магазина) — используй плейсхолдеры из `settings`/ENV и отметь блокер в BUILD_PLAN.

## Definition of Done любой задачи

ruff чист → тесты зелёные, новые тесты на новую логику добавлены → миграция приложена (если менялась схема) → ответы сверены с контрактом по полям → чекбокс задачи в `docs/BUILD_PLAN.md` проставлен → коммит на русском с номером задачи: `M7.2: транзакция создания заказа + idempotency`.

## Стиль

async везде; зависимости через `Annotated`; pydantic v2-схемы отдельно от ORM-моделей; enum статусов/ошибок — единые константы, не строковые литералы по коду; русские докстринги там, где бизнес-логика неочевидна.
