# ТЗ: интеграция Kaspi Pay (Merchant API v2) — v1.0

Приложение к ТЗ v1.0 и `api_logic_v1.md` (раздел 10.1). Исполнитель — Трек A; разделы 8–9 — для Трека B.

**Что уже есть у нас:** договор мерчанта Kaspi под ИП, `TradePointId`, `ApiKey` (ключ магазина), доступ к кабинету.

> ⚠️ **Дисклеймер по источникам.** Раздел собран по актуальным (2026) публичным разборам Merchant API v2, а не по официальной доке Kaspi — она выдаётся в кабинете мерчанта. **Задача №1 Спринта 0:** скачать официальный документ из кабинета, положить в `shop-backend/docs/kaspi/`, сверить имена полей/эндпоинтов с этим файлом и поправить расхождения одним MR. Логика флоу от этого не изменится, имена полей — могут.

---

## 1. Общий флоу

```
Приложение                Наш бэкенд                     Kaspi                GreenKassa
    │ POST /orders             │                            │                     │
    │ POST /orders/{id}/pay ──►│ POST {kaspi}/orders/create │                     │
    │ ◄── payment_url ─────────│ ◄── paymentUrl, kaspiId ───│                     │
    │ открываем Kaspi app      │                            │                     │
    │ (external application)   │       пользователь платит в Kaspi                │
    │                          │ ◄── webhook APPROVED ──────│                     │
    │                          │ payment_status=paid        │                     │
    │                          │ (фоново) ────────────────────────► чек ОФД ──►  │
    │ resumed → поллинг        │                            │      receipt_url    │
    │ GET /orders/{id} ──────► │                            │                     │
    │ ◄── paid + push ─────────│                            │                     │
```

Истина о деньгах — только webhook или прямая сверка статуса у Kaspi. Редиректы и возвраты в приложение — UX, не подтверждение.

---

## 2. Окружения и доступы

| | URL |
|---|---|
| Тест | `https://testpay.kaspi.kz/api/v2` |
| Прод | `https://pay.kaspi.kz/api/v2` |

- Секреты в k8s Secrets: `PAY_KASPI_TRADEPOINT_ID`, `PAY_KASPI_API_KEY` (+ пара для тестового окружения на staging).
- В портале мерчанта до старта: (1) добавить домены `returnUrl`/`failUrl` в белый список — иначе `INVALID_RETURN_URL`; (2) зарегистрировать URL вебхука `https://api.SHOP.kz/api/v1/payments/webhook/kaspi`; (3) запросить у поддержки доступ к тестовому приложению Kaspi для подтверждения оплат на testpay.
- Вебхук обязан отвечать `200` быстро (ориентир — до 5 секунд), иначе Kaspi ретраит. Поэтому в обработчике — только верификация и апдейт статуса; чек, пуши, письма — строго фоном (наш флоу 4.3 это уже соблюдает).

---

## 3. Подпись запросов (HMAC-SHA256)

Значения параметров склеиваются в порядке алфавитной сортировки ключей, подписываются HMAC-SHA256 ключом `ApiKey`:

```python
import hashlib, hmac

def kaspi_signature(params: dict, api_key: str) -> str:
    payload = "".join(str(params[k]) for k in sorted(params))
    return hmac.new(api_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
```

Тот же алгоритм — для верификации подписи входящего вебхука (поле `signature` исключается из расчёта). Несовпало → `403`, `warning` в лог, тело сохраняем в `payments.raw` для разбора.

---

## 4. Используемые методы Kaspi

### 4.1 Создание платежа — `POST /orders/create`

Запрос (Bearer `ApiKey` + `signature` в теле):

| Поле | Значение у нас |
|---|---|
| `tradePointId` | из Secrets |
| `orderId` | **уникален навсегда**: `{order.number}-{attempt}` (см. 4.4) |
| `amount` | `str(order.total)` — целые тенге; минимум 100 ₸ |
| `description` | `Заказ {order.number}, {магазин}` |
| `returnUrl` | `https://api.SHOP.kz/payment/return?order={id}` — серверная страница-заглушка «Вернитесь в приложение» |
| `failUrl` | `https://api.SHOP.kz/payment/fail?order={id}` |

Ответ: `paymentUrl` (отдаём приложению), `orderId` Kaspi (сохраняем в `payments.external_id`).

### 4.2 Статус — `GET /orders/{kaspiOrderId}/status`
Возвращает `PENDING | APPROVED | DECLINED | CANCELLED | REFUNDED`. Используется резервным поллингом (раздел 6) и страницей `returnUrl`.

### 4.3 Возврат — `POST /orders/{kaspiOrderId}/refund`
Полный и частичный (сумма меньше исходной). Требует сохранённый `transactionId` из вебхука → колонка `payments.transaction_id` обязательна.

### 4.4 Повторная оплата после неудачи
`orderId` у Kaspi одноразовый (`ORDER_ALREADY_EXISTS` при повторе). Повторный `POST /orders/{id}/pay` по заказу с `payment_status=failed` создаёт **новую строку в `payments`** с `orderId = {order.number}-{n}`, где n = число попыток + 1. `payment_expires_at` продлевается на 30 мин (если заказ ещё не автоотменён — иначе `409 ORDER_NOT_PAYABLE`).

---

## 5. Маппинг статусов Kaspi → наши

| Kaspi | `payments.status` | `orders.payment_status` | Действия |
|---|---|---|---|
| `PENDING` | pending | pending | ничего |
| `APPROVED` | paid | paid | сверка суммы → фоново: чек GreenKassa, push, уведомление админу |
| `DECLINED` | failed | failed | приложение предлагает «Оплатить ещё раз» |
| `CANCELLED` | failed | failed | то же |
| `REFUNDED` | refunded | refunded | помечаем; инициируется только нашим refund-вызовом |

Все переходы — через единственную идемпотентную функцию `apply_payment_status(payment, kaspi_status, payload)`: и вебхук, и поллинг, и `returnUrl`-страница зовут её; повторное применение того же статуса — no-op.

---

## 6. Надёжность: вебхук + резервный поллинг

Вебхуки могут задерживаться или теряться — оплата не должна «зависать»:

1. **Вебхук** (основной канал): верификация подписи → идемпотентность по `external_id` (`payments.external_id` уже `paid` → сразу `200 {ok}`) → сверка `payload.amount == order.total` (не сошлось → НЕ подтверждаем, алерт) → `apply_payment_status` → `200`.
2. **Резервный поллинг (сервер)**: после `create_payment` Arq-задача опрашивает `GET /orders/{kaspiId}/status` — через 1 мин, далее каждые 2 мин до `payment_expires_at`. Терминальный статус → та же `apply_payment_status`. Пропавший вебхук больше не проблема.
3. **Гонка с автоотменой** (флоу 4.4 логики): обе стороны берут `SELECT … FOR UPDATE` строки заказа. `APPROVED` по уже отменённому заказу → `payments.status=paid`, заказ не реанимируем, алерт «оплата по отменённому — сделать возврат» (кнопка refund в админке).

---

## 7. KaspiPayAdapter — скелет

```python
class KaspiPayAdapter(PaymentProvider):
    async def create_payment(self, order, attempt: int) -> Payment: ...
        # POST /orders/create → payments(external_id, status=pending, amount=order.total)
    async def get_status(self, payment) -> KaspiStatus: ...
        # GET /orders/{external_id}/status
    def parse_webhook(self, raw: bytes, headers) -> WebhookEvent | InvalidSignature: ...
        # верификация kaspi_signature, → {external_id, status, amount, transaction_id, raw}
    async def refund(self, payment, amount: int) -> RefundResult: ...
        # POST /orders/{external_id}/refund; требует payment.transaction_id
```

Юнит-тесты обязательны: подпись (эталонный вектор), маппинг статусов, идемпотентность `apply_payment_status`, повторный вебхук, вебхук с неверной суммой, `ORDER_ALREADY_EXISTS` → новая попытка.

---

## 8. Мобильная часть (Трек B)

1. `POST /orders/{id}/pay` → `payment_url` → `launchUrl(..., mode: LaunchMode.externalApplication)` — ссылку перехватывает установленное приложение Kaspi.kz; без него откроется браузер (fallback `inAppBrowserView`).
2. Возврат ловим **не по deeplink**, а по `AppLifecycleState.resumed` (`WidgetsBindingObserver` на экране ожидания оплаты): resumed + «ждём оплату» → поллинг `GET /orders/{id}` каждые 2 с, до 90 с.
3. Экран ожидания: лоадер «Ждём подтверждение от Kaspi», по `paid` → экран успеха (+чек появится в заказе чуть позже), по `failed` → «Оплата не прошла» с кнопкой «Повторить», по таймауту → «Проверим и пришлём push» (вебхук/поллинг сервера дожмут, придёт push).
4. Кнопка оплаты в чекауте: логотип и текст «Оплатить через Kaspi.kz» — единственный метод, радиогруппа не нужна, пока метод один.

---

## 9. Возвраты и отмены оплаченных

- Админ-отмена оплаченного заказа (`POST /admin/orders/{id}/cancel`): транзакция отмены (остатки, бонусы) + `refund(payment, order.total)` + при успехе Kaspi пришлёт/вернёт `REFUNDED`. Ошибка возврата → заказ остаётся оплаченным, алерт, ручной повтор из админки.
- Частичные возвраты в MVP не делаем (только полная отмена) — частичный `refund` оставлен в адаптере на будущее.
- По чекам: возврат = чек возврата в GreenKassa (той же фоновой задачей, тип «возврат продажи»).

---

## 10. Рассрочка Kaspi (вторая очередь, фича-флаг)

`orders/create` принимает `installments: [3, 6, 12]` — покупателю на странице Kaspi предлагается оплата сразу или рассрочка; мерчант получает всю сумму (минус комиссия). Вебхук приходит с `paymentType: INSTALLMENT` и `installmentPeriod`. Требует отдельной активации у менеджера Kaspi (`INSTALLMENT_NOT_ALLOWED` = не включена). Для канцтоваров имеет смысл на корзинах от ~30 000 ₸ (школьные наборы, рюкзаки, оптовые закупки к сентябрю) — включим флагом `kaspi_installments_enabled` после MVP, контракт менять не придётся.

---

## 11. Ошибки Kaspi → наша реакция

| Код Kaspi | Причина | Реакция |
|---|---|---|
| `INVALID_SIGNATURE` | порядок/алгоритм подписи | баг у нас; эталонный тест подписи должен ловить до прода |
| `ORDER_ALREADY_EXISTS` | повтор `orderId` | новая попытка `{number}-{n}` (раздел 4.4) |
| `TRADE_POINT_NOT_FOUND` | кривой TradePointId | проверка Secrets на старте приложения (fail-fast healthcheck) |
| `AMOUNT_TOO_SMALL` | сумма < 100 ₸ | серверная валидация в `/pay`: `TOTAL_TOO_SMALL` → приложение просит добрать корзину |
| `INVALID_RETURN_URL` | домен не в белом списке | добавить домен в портале мерчанта (чек-лист) |
| сеть/5xx Kaspi | недоступность | ретрай create ×3 с backoff; дальше `502` приложению «Kaspi временно недоступен, попробуйте позже» |

---

## 12. Тестирование на testpay (чек-лист)

- [ ] Успешная оплата: create → тестовое приложение Kaspi → вебхук APPROVED → paid → чек → push
- [ ] Отклонённая оплата → failed → «Оплатить ещё раз» → новая попытка проходит
- [ ] Вебхук потерян (заглушить endpoint) → резервный поллинг дожимает paid
- [ ] Повторный вебхук APPROVED → no-op, бонусы/чек не задвоились
- [ ] Вебхук с неверной подписью → 403; с неверной суммой → не подтверждено + алерт
- [ ] Автоотмена через 30 мин; оплата после автоотмены → алерт «сделать возврат»
- [ ] Полный refund из админки → REFUNDED, чек возврата
- [ ] Kaspi недоступен на create → корректная ошибка в приложении

## 13. Чек-лист выхода в прод

- [ ] Официальная дока из кабинета сверена, расхождения полей исправлены
- [ ] Домены returnUrl/failUrl в белом списке прод-портала; вебхук-URL зарегистрирован
- [ ] `transaction_id` сохраняется; сверка суммы включена; поллинг-fallback включён
- [ ] Секреты прод/тест разведены по окружениям k8s
- [ ] Переключение `testpay.kaspi.kz` → `pay.kaspi.kz` только через ENV
- [ ] Контрольная боевая оплата на 100–200 ₸ своей картой + возврат
