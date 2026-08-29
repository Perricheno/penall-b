# Логика API v1.0 — связи, состояния, флоу

Приложение к ТЗ v1.0 и `openapi.yaml`. Это документ, который кладётся Claude в контекст вместе с контрактом при разработке любой фичи бэкенда. Мобильному треку — разделы 2, 3, 4, 6, 8.

---

## 1. Связи сущностей (ER)

```
users 1───1 bonus_accounts
users 1───N orders ─── 1───N order_items ───N───1 products
users 1───N bonus_transactions (order_id nullable — для adjust/expire)
users 1───N devices
users 1───N promo_usages ───N───1 promo_codes
users M───N products (favorites)
categories 1───N categories (parent_id, глубина строго 2)
categories(2-й уровень) 1───N products
orders 1───N payments (попытки оплаты; успешная — одна)
settings, bonus_tiers, banners — справочники без связей
idempotency_keys N───1 orders
import_logs — журнал, без FK
```

**Инварианты:**
- Товар привязывается только к подкатегории (2-й уровень). Выборка по корневой = по всем её детям.
- `order_items` — снапшот: name, price, image фиксируются на момент создания заказа. Изменение/скрытие товара не трогает историю.
- Товар не удаляется физически никогда (`is_active=false`) — на него ссылаются заказы.
- `bonus_accounts.balance` меняется **только** одновременно с записью в `bonus_transactions`, в одной транзакции БД. Сверка: balance == Σ(transactions.amount).
- `orders.bonus_earned` фиксируется при создании заказа (по уровню на этот момент) — чтобы совпадать с плашкой, которую видел пользователь. При `done` просто зачисляется.

**Уточнения схемы БД относительно ТЗ (раздел 10) — внести при написании миграций:**

```
payments(id, order_id FK, provider, external_id UNIQUE, amount INT,
         status, raw JSONB, created_at)                  -- каждая попытка оплаты
idempotency_keys(key UUID PK, user_id, order_id, created_at)
bonus_transactions: + spent_amount INT DEFAULT 0         -- для FIFO-списания (только type=accrual)
                    + status (active|expired)            -- только type=accrual
users: + is_blocked BOOL DEFAULT false
products: + stock_source ENUM('umag','manual') DEFAULT 'manual'
payments: + transaction_id TEXT NULL             -- kaspi transactionId, для refund/споров
```

---

## 2. Деньги: порядок расчёта (канон)

Единственная реализация — функция `calculate_cart()`. Её используют и `POST /cart/calculate`, и `POST /orders`. Дублирования логики нет нигде, включая клиент: приложение только отображает ответ сервера.

Порядок, все округления — `floor` до целого тенге:

```
1. items_total     = Σ(server_price × qty)                       # только available-позиции
2. promo_discount  = percent: floor(items_total × v/100)
                     fixed:   min(v, items_total)
                     если промокод невалиден → 0, promo.applied=false
3. base            = items_total − promo_discount
4. bonus_max       = floor(base × redeem_limit% / 100)           # settings, дефолт 30
   bonus_spent     = min(bonus_to_spend, bonus_max, balance)     # гость → 0
5. delivery_price  = pickup  → 0
                     courier → base ≥ courier_free_from ? 0 : courier_price
                     kazpost → KazpostAdapter.calc_tariff(destination, Σ weight_g)
                               (destination не задан → null, заказ не оформить)
6. total           = base − bonus_spent + delivery_price
7. bonus_will_earn = floor(tier% × (base − bonus_spent) / 100)   # уровень по lifetime_spent
                                                                 # на доставку бонусы не начисляются
```

**Проверочный пример** (уровень 3%, промо 10%, лимит списания 30%, доставка 1500/бесплатно от 20 000):

```
корзина 12 000 ₸ → promo 1 200 → base 10 800
bonus_max = 3 240; юзер списывает 3 000
delivery: 10 800 < 20 000 → 1 500
total = 10 800 − 3 000 + 1 500 = 9 300
bonus_will_earn = floor(3% × 7 800) = 234
lifetime_spent после done: +7 800 (товарная часть деньгами, без доставки)
```

Этот пример — обязательный unit-тест `test_calculate_canonical()`.

---

## 3. Машины состояний

### 3.1 Статус заказа

```
              ┌────────────────────────────────────────────┐
new ──► confirmed ──► assembling ──┬─► ready_for_pickup ──►│ done
 │                                 └─► delivering ─────────►│
 └──► cancelled ◄── (из new/confirmed/assembling — только админ)
```

| Переход | Кто | Условие | Побочные эффекты |
|---|---|---|---|
| → new | система (POST /orders) | — | резерв остатков, redeem бонусов, фикс. bonus_earned, promo_usage |
| new → confirmed | админ | payment_status=paid | push «Заказ подтверждён» |
| confirmed → assembling | админ | — | — |
| assembling → ready_for_pickup | админ | delivery_type=pickup | push «Заказ готов к выдаче» |
| assembling → delivering | админ | courier / kazpost; для kazpost track_number обязателен в PATCH | push «Передан курьеру» / «Отправлен Казпочтой, трек …» |
| ready_for_pickup / delivering → done | админ | — | **начисление bonus_earned**, lifetime_spent += (base − bonus_spent), push «+N бонусов» |
| new → cancelled | пользователь или админ | can_cancel | возврат остатков, revert бонусов/промо; если paid → refund |
| confirmed / assembling → cancelled | только админ | — | то же + refund через провайдера, push |
| new → cancelled (auto) | фоновая задача | online, не оплачен 30 мин | возврат остатков, revert бонусов/промо |

Любой другой переход → `409 INVALID_STATUS_TRANSITION`. Таблица переходов — словарь-константа в коде, покрыта тестом полностью.

### 3.2 Статус оплаты

```
pending ──► paid ──► refunded
    └─────► failed ──► pending (повторный /pay: новая попытка в payments)
```

`paid` и `refunded` выставляются **только** обработчиком webhook / refund-вызовом к провайдеру. Никогда — по редиректу из браузера.

---

## 4. Ключевые флоу

### 4.1 Вход по OTP
```
app → POST /auth/otp/request {phone, channel=whatsapp}
  сервер: rate limit (Redis: otp:req:{phone}, 3/час; otp:req:ip:{ip}, 10/час)
          код 4 цифры → OtpChannel: сначала WhatsApp (auth-шаблон через BSP);
          нет WhatsApp / не доставлен / канал выключен → фолбэк SMS;
          фактический канал возвращается в ответе (UI: «Код отправлен в WhatsApp»)
          в БД bcrypt(code), expires 5 мин
app → POST /auth/otp/verify {phone, code}
  сервер: ≤5 попыток (attempts++), сверка хэша
          нет user → создать + bonus_account(balance=0)
          → JWT-пара, refresh в БД (ротация: старый отзывается при /auth/refresh)
```

### 4.2 Создание заказа (`POST /orders`) — самая важная транзакция
```
1. Idempotency-Key есть в idempotency_keys? → вернуть существующий заказ, 200. Стоп.
2. calculate_cart(payload) → totals
3. totals.total ≠ client_total ИЛИ есть unavailable → 409 PRICE_CHANGED / OUT_OF_STOCK
   (в details — полный свежий calculate, приложение показывает «Цены обновились»)
4. BEGIN;
     SELECT … FROM products WHERE id IN (…) FOR UPDATE;   -- фикс. порядок id (защита от deadlock)
     повторная проверка stock ≥ qty  → иначе ROLLBACK, 409 OUT_OF_STOCK
     UPDATE products SET stock = stock − qty;
     INSERT orders (status=new, payment_status=pending,
                    payment_expires_at = now()+30m,
                    bonus_earned = totals.bonus_will_earn, …);
     INSERT order_items (снапшоты);
     если bonus_spent > 0: FIFO-redeem (см. 4.6) + transaction(type=redeem);
     если промокод: INSERT promo_usages, used_count++;
     INSERT idempotency_keys;
   COMMIT;
5. ответ 201 Order
```

### 4.3 Оплата
```
app → POST /orders/{id}/pay
  сервер: PaymentProvider.create_payment(order) → INSERT payments(status=pending) → payment_url
app: Custom Tab → пользователь платит → редирект app://payment-result?order_id=
app: поллинг GET /orders/{id} каждые 2с до 60с

провайдер → POST /payments/webhook/{provider}
  1. verify_signature(raw_body) → нет → 403, алерт в лог
  2. external_id уже status=paid в payments? → 200 {ok}. Стоп (идемпотентность).
  3. BEGIN; payments.status=paid; orders.payment_status=paid; COMMIT;
  4. → 200 провайдеру СРАЗУ (не ждём чек!)
  5. фоново (Arq): фискализация ОФД → receipt_url; push «Оплата прошла»; уведомление админу
```
Чек упал → ретрай задачи (5 попыток, backoff), алерт после исчерпания. Оплата при этом уже подтверждена — чек догоняет.

### 4.4 Автоотмена неоплаченных
```
Arq cron */5 мин: orders WHERE payment_status=pending AND payment_expires_at < now()
  на каждый: транзакция { status=cancelled; вернуть stock; revert бонусов/промо }
Гонка «webhook пришёл в ту же секунду»: обе операции берут SELECT … FOR UPDATE на строку заказа;
webhook по уже cancelled заказу → payments.status=paid, алерт админу «оплата по отменённому» (ручной refund).
```

### 4.5 Начисление при `done`
```
транзакция {
  bonus_accounts.balance += order.bonus_earned
  INSERT bonus_transactions(type=accrual, amount=+earned,
                            expires_at = today + settings.expiry_days, spent_amount=0)
  bonus_accounts.lifetime_spent += (items_total − promo_discount − bonus_spent)
}
push «Начислено N бонусов за заказ ORD-…»
Смена уровня применяется к СЛЕДУЮЩИМ заказам автоматически (lookup по lifetime_spent).
```

### 4.6 FIFO списания и сгорание
```
redeem X: активные accrual-лоты ORDER BY expires_at ASC:
          spent_amount += min(остаток лота, сколько ещё нужно); balance −= X;
          одна транзакция type=redeem на сумму −X.
revert (отмена заказа): balance += X, transaction(type=revert, +X);
          новый «лот» со сроком = max(исходных) — упрощение, допустимо.
expire (Arq cron 03:00): лоты expires_at < today AND amount − spent_amount > 0:
          остаток → transaction(type=expire, −остаток), balance −= остаток, status=expired.
напоминание (cron 10:00): лоты, сгорающие через 7 дней → push «Сгорит N бонусов DD.MM».
```

### 4.7 XLSX-импорт
```
1. POST /admin/products/import?dry_run=true — файл в память, построчная валидация:
   sku непустой и уникальный в файле; category_path существует; price>0; stock≥0;
   old_price>price или пусто; attr:* колонки → attributes.
2. Отчёт: created/updated/errors[{row, sku, error}]. Админ смотрит.
3. Повтор с dry_run=false: батчами по 500 в транзакции; upsert по sku
   (существует → обновить все поля из файла, кроме images; нет → создать).
   Ошибочные строки пропускаются, попадают в отчёт. INSERT import_logs.
4. После импорта: bump catalog:ver (см. раздел 7).
Файл 4000 строк — цель < 2 мин (критерий приёмки №8).
```

---

## 5. Логика по эндпоинтам (сервер)

Формат: **валидации → действия → кэш**. Пагинация, auth и коды ошибок — по контракту, тут только неочевидное.

**GET /categories** — из кэша `cat:tree:{ver}`; только is_active, сортировка sort; дерево собирается одним запросом.

**GET /products** — category_id корневой → расширить до детей; search → `websearch_to_tsquery('russian')` OR `similarity(name, q) > 0.25` OR `sku ILIKE`; ранжирование: ts_rank DESC, similarity DESC; attrs → `attributes @> jsonb`-фильтры по КВ-парам (см. примечание ниже); сортировка popular = sales_count DESC; **всегда** `ORDER BY (stock = 0), <sort>` — нули в конце; bonus_amount = floor(price × tier%/100) уровня запросившего (гость → тир с threshold=0); is_favorite — один LEFT JOIN. Кэш только для запросов без auth-зависимых полей нельзя — поэтому кэшируем «сырую» выборку id по ключу запроса (60 сек), карточки добираем из `prod:{id}`, bonus/favorite считаем поверх.

> Примечание: в БД attributes хранится как JSONB-объект `{"Бренд":"X"}` для фильтров `@>`; в API наружу отдаётся массивом пар (порядок отображения = порядок ключей при импорте, храним отдельным полем `attr_order text[]`).

**GET /products/facets** — по активным товарам категории: min/max цены; ключи из attr_order, значения с count; кэш `facets:{category_id}:{ver}` 10 мин.

**GET /products/{id}** — из `prod:{id}` кэша + similar: та же подкатегория, stock>0, цена ∈ [0.6p, 1.4p], ORDER BY sales_count DESC LIMIT 10 (кэш `similar:{id}:{ver}` 10 мин).

**GET /search/suggest** — queries: топ-5 популярных запросов с префиксом q (таблица search_queries, пишем туда каждый непустой поиск — счётчик); products: топ-5 по similarity.

**POST /cart/calculate** — раздел 2. Невалидный промокод не роняет ответ. `qty > stock` → qty урезается до stock, это видно по items[].qty ≠ запрошенному.

**POST /orders** — флоу 4.2. Rate limit 10 заказов/час/юзер.

**POST /orders/{id}/cancel** — только владелец, только status=new; транзакция как в 4.4.

**POST /orders/{id}/pay** — payment_status ∈ {pending, failed}; failed → создаётся новая попытка в payments, payment_expires_at продлевается на 30 мин от сейчас (но заказ мог уже автоотмениться — тогда 409 ORDER_NOT_PAYABLE).

**DELETE /me** — deleted_at=now(); phone → `del:{id}:{random}`; name/email/birth_date → null; refresh-токены отозваны; devices удалены; заказы и бонус-транзакции остаются (учёт/налоги).

**PATCH /admin/orders/{id}/status** — по таблице 3.1, эффекты там же. Всё в транзакции.

**PUT /admin/products/{id} и import** — после коммита: bump `catalog:ver`, DEL `prod:{id}`.

**POST /admin/products/bulk** — price_percent: `price = greatest(1, floor(price × (100+v)/100))`; в один UPDATE; bump ver.

**GET /admin/orders/{id}/invoice** — генерация XLSX накладной З-2 существующим openpyxl-пайплайном из order_items-снапшотов; отдаём стримом, на диск не пишем.

---

## 6. Реестр кодов ошибок

| Код | HTTP | Где | Действие приложения |
|---|---|---|---|
| VALIDATION_ERROR | 422 | везде | подсветить details.fields |
| RATE_LIMITED | 429 | otp, orders | таймер details.retry_after |
| TOKEN_EXPIRED / TOKEN_INVALID | 401 | защищённые | тихий refresh → повтор; иначе на экран входа |
| FORBIDDEN | 403 | admin, чужие ресурсы | — |
| NOT_FOUND | 404 | {id} | заглушка «не найдено» |
| OTP_INVALID / OTP_EXPIRED / OTP_ATTEMPTS_EXCEEDED | 400 | verify | текст под полем кода |
| PRICE_CHANGED | 409 | orders | перерисовать корзину из details, тост «Цены обновились» |
| OUT_OF_STOCK | 409 | orders | то же |
| ORDER_NOT_CANCELLABLE / ORDER_NOT_PAYABLE / ALREADY_PAID | 409 | orders | обновить экран заказа |
| INVALID_STATUS_TRANSITION | 409 | admin | тост |
| PROMO_NOT_FOUND / PROMO_EXPIRED / PROMO_MIN_ORDER / PROMO_LIMIT_REACHED / PROMO_USER_LIMIT | — | внутри calculate (promo.error_code, HTTP 200) | текст под полем промокода |
| BONUS_LIMIT_EXCEEDED | — | внутри calculate (bonus.applied < requested) | приложение показывает applied |
| SKU_EXISTS / CATEGORY_NOT_EMPTY | 409 | admin | тост |
| USER_BLOCKED | 403 | verify, orders | экран «Аккаунт заблокирован, свяжитесь с поддержкой» |

Правило: message всегда человекочитаем на русском — приложение может показывать его как есть.

---

## 7. Кэш (Redis)

| Ключ | Что | TTL | Инвалидация |
|---|---|---|---|
| `catalog:ver` | int-версия каталога | ∞ | INCR при любом изменении товаров/категорий/импорте |
| `cat:tree:{ver}` | дерево категорий | 24ч | через ver |
| `prod:{id}` | карточка (без bonus/favorite) | 1ч | DEL при апдейте товара |
| `list:{md5(нормализованный query)}:{ver}` | id выдачи листинга | 60с | через ver |
| `facets:{cat}:{ver}`, `similar:{id}:{ver}` | фасеты, похожие | 10 мин | через ver |
| `cfg` | /config | 5 мин | DEL при PUT settings / tiers |
| `otp:req:{phone}`, `otp:req:ip:{ip}`, `ord:rl:{user}` | rate limits | окно | — |

Паттерн с `{ver}` в ключе избавляет от массового DEL: старые ключи просто протухают.

---

## 8. Push-уведомления (FCM)

| Триггер | Текст (RU) | Deeplink |
|---|---|---|
| payment paid | «Оплата прошла ✓ Заказ {number} принят» | app://orders/{id} |
| status confirmed | «Заказ {number} подтверждён» | app://orders/{id} |
| status ready_for_pickup | «Заказ {number} готов к выдаче» | app://orders/{id} |
| status delivering | «Заказ {number} передан курьеру» | app://orders/{id} |
| status done | «Спасибо за покупку! +{n} бонусов» | app://bonuses |
| status cancelled (админом) | «Заказ {number} отменён. Подробности в поддержке» | app://orders/{id} |
| бонусы сгорают через 7 дн | «{n} бонусов сгорит {date}. Успейте потратить» | app://bonuses |

Отправка — всегда фоновой задачей (не в HTTP-обработчике). Битые токены (UNREGISTERED) удаляются из devices.

---

## 9. Изменения относительно ТЗ v1.0 (внести в ТЗ следующим MR)

1. **Убран `POST /promo/validate`** — промокод проверяется внутри `/cart/calculate` (promo.error_code), меньше эндпоинтов и один источник расчёта.
2. **Добавлены**: `GET /config` (публичный конфиг: контакты, тарифы, уровни бонусов, оферта, min_app_version), `GET /products/facets` (данные для шторки фильтров), `GET /admin/imports`, `POST /admin/uploads`, `POST /admin/users/{id}/block`.
3. **`POST /orders`**: обязательные `Idempotency-Key` (header) и `client_total` (защита от «оформил по старой цене»).
4. **Webhook** → `POST /payments/webhook/{provider}` (путь с провайдером — под возможные два провайдера).
5. **Схема БД**: + таблицы `payments`, `idempotency_keys`; + `bonus_transactions.spent_amount/status` (FIFO-лоты); + `users.is_blocked`; attributes хранится JSONB-объектом + `attr_order`.
6. **PaymentStatus**: добавлено значение `cod` для заказов с оплатой при получении.

---

## 10. Слой интеграций (Дополнение №1)

Принцип: всё внешнее — за интерфейсами-адаптерами. Ядро (заказы, деньги, бонусы) не знает названий банков и служб доставки. Новый банк / доставка / учётка = новый адаптер + флаг в settings, контракт и ядро не трогаем.

### 10.1 PaymentProvider — Kaspi Pay (единственный в MVP)

```
interface PaymentProvider:
  create_payment(order)                → {payment_url, external_id}
  parse_webhook(provider, raw, hdrs)   → {external_id, status, amount} | InvalidSignature
  refund(payment)                      → ok | error
```

- Единственная реализация MVP — `KaspiPayAdapter` (Merchant API v2): договор, TradePointId и ApiKey под ИП уже есть → блокеров по доступам нет. Полное ТЗ интеграции — `kaspi_pay_integration.md` (окружения, подпись HMAC-SHA256, вебхук + резервный поллинг, возвраты, чек-листы).
- Интерфейс сохраняем: другой банк в будущем = новый адаптер + MR в enum контракта, ядро не трогается.
- Ключи — в k8s Secrets (`PAY_KASPI_TRADEPOINT_ID`, `PAY_KASPI_API_KEY`), тест и прод разведены.
- **Проверка суммы обязательна:** `webhook.amount == order.total`, иначе оплату не подтверждаем, алерт админу.
- В `payments` добавлен `transaction_id` — без него у Kaspi не сделать возврат.

### 10.2 DeliveryProvider — Казпочта

```
interface DeliveryProvider:
  calc_tariff(destination, weight_g)   → price          # вызывается из calculate_cart
  create_shipment(order)               → track_number   # при assembling → delivering
  track(track_number)                  → status         # cron 1/час
```

- Вес заказа = Σ `weight_g` позиций (нет веса → `default_weight_g` из настроек). Тариф кэшируется: `kp:tariff:{index}:{weight_bucket}` (бакеты по 100 г), TTL 24 ч — иначе задолбим их API из корзины.
- Тариф **включается в total и оплачивается клиентом** вместе с заказом (единый чек ОФД). Бонусы на доставку не начисляются — раздел 2 без изменений.
- Graceful degradation: пока нет API-доступа к созданию отправлений, админ вводит трек руками в `PATCH /admin/orders/{id}/status` — контракт это уже позволяет. `track()` по кроне обновляет статус в деталях заказа; в `done` автоматически не переводим, только подсказка админу.

### 10.3 OtpChannel — вход через WhatsApp

```
interface OtpChannel: send(phone, code) → delivered_channel
```

- Основной канал — **официальный WhatsApp Business Platform** через BSP (360dialog, Wazzup и т.п.), шаблон категории Authentication. Нужен верифицированный WABA — заявка в Спринт 0, одобрение занимает от нескольких дней.
- Фолбэк — SMS (Mobizon/SMSC), навсегда: WhatsApp есть не у всех, и до одобрения WABA живём только на SMS (фича-флаг `otp_whatsapp_enabled`).
- Неофициальные шлюзы (Green API поверх обычного номера) в прод не берём: риск бана номера — а на нём же поддержка магазина.

### 10.4 catalog_sync — 1С и uMag (вторая очередь, но архитектурно готово сейчас)

- Единая точка входа — сервис `catalog_sync.upsert(rows, source)`. XLSX-импорт **уже** ходит через него → коннекторы 1С/uMag просто приводят свои данные к тому же формату строк.
- **uMag — остатки и цены теперь в MVP.** Публичной документации API у uMag нет — доступ и доки запрашиваем у их поддержки в Спринт 0 (готовые интеграции у них есть, значит API живой). До получения — полуавтомат: регулярный XLSX-экспорт из uMag → наш готовый импорт, тот же `catalog_sync`. Матчинг по **штрихкоду**, расписание раз в 10–15 мин + кнопка «Синхронизировать сейчас» в админке.
- **Ручной оверрайд остатка:** `products.stock_source: umag | manual`. Синк трогает только `stock_source=umag`; ручная правка остатка в админке автоматически переводит товар в `manual` (обратно — тумблер в форме товара). Товары без штрихкод-матча — всегда `manual`.
- **Правда об остатках:** офлайн-продажи знает uMag, онлайн-резервы — мы. Синк пишет `stock = max(0, umag_qty − qty товара в открытых онлайн-заказах)`. Операционное правило магазина: онлайн-заказ пробивается на кассе uMag при сборке/выдаче — тогда остатки в uMag остаются истиной и двойного счёта нет.
- **1С:** OData (УТ/Розница) или файловый CommerceML — выбираем, когда появится конкретная конфигурация.
- Направление в MVP-архитектуре — только inbound (к нам). Экспорт онлайн-продаж в учётку — отдельная задача после запуска.
- Конфликты решает настройка «источник-владелец поля» в settings: например, цены и остатки — из uMag, описания/фото/характеристики — из админки. Импорт не затирает чужие поля.

### 10.5 FiscalProvider — GreenKassa (ОФД-чеки)

- Облачная ККМ + ОФД одним тарифом (~15 000 ₸/год), заявлен открытый REST API — доки и доступ берём при подключении (greenkassa.kz / кабинет BusinessGO). Регистрация онлайн-кассы под ИП — Спринт 0.
- Интерфейс `FiscalProvider.create_receipt(order, kind: sale|refund) → receipt_url` — вызывается фоновой задачей после `paid` (флоу 4.3 не меняется). Позиции чека — из `order_items`-снапшотов, доставка — отдельной строкой чека.
- Ретраи ×5 с backoff, алерт после исчерпания; оплата уже подтверждена — чек догоняет.
- Возврат заказа = чек возврата той же задачей.

---

## 11. Дополнение №1 к changelog (внести в ТЗ)

7. OTP уходит в **WhatsApp** (официальный BSP, auth-шаблон) с автофолбэком на SMS; в `/auth/otp/request` появился `channel`.
8. `payment_method` → enum `kaspi | halyk | forte | cod`; список методов чекаута — из `/config`, включаются фича-флагами.
9. Доставка: `delivery_type` → `pickup | courier | kazpost`. Для Казпочты: `destination{city, index}` в calculate, `weight_g` у товара (+ колонка импорта), `track_number` у заказа, тариф в total (платит клиент).
10. 1С / uMag — через единый `catalog_sync` (upsert по SKU/штрихкоду, общий путь с XLSX-импортом).

**Дополнение №2 (закрытие открытых вопросов ТЗ, 14.08):**
11. Платёжный провайдер MVP — **только Kaspi Pay** (договор/ключи есть); `payment_method` → `[kaspi]`, Halyk/Forte удалены из контракта — вернутся отдельным MR, если понадобятся. ТЗ интеграции — `kaspi_pay_integration.md`.
12. **«Оплата при получении» исключена**: флоу жёсткий — оплата → сбор → передача. `cod` удалён из PaymentStatus и всех enum; автоотмена 30 мин действует на все заказы.
13. Остатки — **синк из uMag в MVP** + ручной оверрайд (`products.stock_source`), см. 10.4.
14. ОФД — **GreenKassa** (см. 10.5) вместо WebKassa/reKassa.
15. Прод-хостинг — **PS.kz** (локализация ПДн в РК закрыта); staging остаётся на текущем VPS.
16. Источник каталога: номенклатура/цены/остатки — uMag; фото и описания — разовая миграция из карточек Каспи (токен Магазина на Kaspi.kz есть) + догрузка через админку.
