# Tera backend

Python 3.12 · PostgreSQL · Redis · FastAPI. Yadro (`core`, `ledger`, `balances`, `modules`, `cache`)
faqat standart kutubxonadan foydalanadi; `psycopg` va `redis` faqat PostgreSQL/Redis bilan ishlaganda kerak.

## Testlar

Repozitoriy ildizidan:

    # 1) Tezkor, hech narsa o'rnatmasdan (SQLite xotirada):
    python -m unittest discover -s backend/tests -t .

    # 2) To'liq, PostgreSQL va haqiqiy Redis bilan (Docker kerak). Shu ishlab chiqarishga mos tekshiruv:
    docker compose -f infrastructure/docker/docker-compose.yml --profile test run --rm tests

    # 3) Docker'siz, o'z PostgreSQL/Redis'ingiz bilan:
    pip install -r backend/requirements.txt
    TERA_TEST_DB=postgresql://tera:tera@localhost:5433/tera_test TERA_TEST_REDIS=redis://localhost:6380/15 \
        python -m unittest discover -s backend/tests -t .

DIQQAT: `TERA_TEST_DB` bazasi har testdan oldin TOZALANADI. Nomi `_test` bilan tugamasa, tozalash rad etiladi.

## Tuzilma

- `core/`      pul, vaqt, xatolar, sozlamalar (`policy.py`, `config.py`), tekshiruv yordamchilari
- `ledger/`    hodisalar jurnali, baza qatlami (`db.py`: SQLite va PostgreSQL), migratsiyalar
- `balances/`  qoldiq va hujjatlar keshi, jurnaldan qayta qurish
- `cache/`     Redis vazifalari: kesh, tezlik cheklovi, qulf, oqimlar (xotirada va haqiqiy Redis)
- `modules/`   jarayonlar: `auth`, `sync` (dvigatel), `reference`, `pricing`, `policy`, `approvals`
- `api/`      FastAPI kirish qatlami va hisobot endpointlari
- `jobs/`     tungi audit va outbox relay jarayonlari
- `telegram_bot/`  egaga Telegram xabarlarini yetkazish

## Baza qoidalari

- Ishlab chiqarishda faqat PostgreSQL. SQLite faqat sinov va tezkor ishlab chiqish uchun.
- Har yozuvchi tranzaksiya ketma-ket bajariladi (SQLite: `BEGIN IMMEDIATE`, PostgreSQL: `pg_advisory_xact_lock`).
  Hash zanjiri va biznes qoidalari (qoldiq, qarz limiti) shunga tayanadi.
- Migratsiyalar ikki joyda va bir-biriga MOS bo'lishi shart: `ledger/schema.py` (SQLite) va
  `ledger/migrations/postgres/NNN_*.sql`. Mosligini `test_stage4b_database.py` tekshiradi.
- Pul va qoldiq `BIGINT`. Hodisa mazmuni `TEXT` (JSONB emas): hash aynan shu matnga tayanadi.

## Redis qoidasi

Redis hech qachon haqiqat manbai emas. Pul, qoldiq, hodisalar va xabar navbati (`outbox`) faqat PostgreSQL'da.
Redis o'chsa: kesh "topilmadi" deydi, tezlik cheklovi o'tkazib yuboradi (bazadagi bloklash ishlayveradi),
Redis qulfini talab qiladigan amal xato beradi, xabarlar bazada kutib turadi.

## Sozlamalar

`infrastructure/env/.env.example` ni nusxalab `infrastructure/env/.env` yarating va
`TERA_DATABASE_URL`, `TERA_REDIS_URL`, `TERA_SECRET` ni to'ldiring. Telegram xabarnomasi uchun
`TELEGRAM_BOT_TOKEN` va `TELEGRAM_OWNER_CHAT_ID` ham kerak.

## Bosqichlar

1. Poydevor: `core/`.
2. Hodisalar jurnali: `ledger/`, `balances/`.
3. Buyruq dvigateli va kirish: `modules/auth/`, `modules/sync/`. Ruxsatlar: `shared/constants/permissions.json`.
4. Ma'lumotnoma, narx, siyosat, ruxsatlar: `modules/reference|pricing|policy|approvals/`.
4b. PostgreSQL va Redis: baza qatlami, migratsiyalar, `cache/`, Docker, sozlamalar.
5. Xarid: `modules/purchase/` — xom ashyo va qadoq xaridi, o'rtacha tannarx, yo'l xarajati va yetkazib beruvchi qarzi.
6. Qadoqlash: `modules/packaging/` — aralash partiya, xom ashyo va qadoq sarfi, tannarx, yaroqlilik va kam chiqish statistikasi.
7. Sklad va yuklash: `modules/warehouse/` — partiyalarni FIFO yuklash, agent zimmasi, ikki tomonlama tasdiq va inventarizatsiya.
8. Sotuv va qarz: `modules/sales/` — narx nusxasi, chegirma/limit ruxsatlari, agentdagi FIFO qoldiq, do'kon qarzi, to'lov, muddat va bahs.
9. Qaytim va kun yopish: `modules/returns/`, `modules/closing/` — qaytim hisobi, almashtirish, karantin/write-off, agent balanslari va naqd topshirish.
10. Hisobotlar va ishga tushirish: `modules/reporting/`, `api/`, `jobs/`, `telegram_bot/`.

## API va xizmatlarni ishga tushirish

`infrastructure/env/.env.example` dan `infrastructure/env/.env` yarating, `TERA_SECRET` ni
kamida 32 baytli tasodifiy qiymatga almashtiring. Repozitoriy ildizidan:

    docker compose --env-file infrastructure/env/.env -f infrastructure/docker/docker-compose.yml up -d postgres redis api
    docker compose --env-file infrastructure/env/.env -f infrastructure/docker/docker-compose.yml exec api python -m backend.cli bootstrap-owner --login owner --full-name "Tizim egasi"

Birinchi buyruq API'ni `http://localhost:8000` da ishga tushiradi; OpenAPI interfeysi `/docs` da.
`bootstrap-owner` parolni terminalda maxfiy so'raydi va foydalanuvchi bor tizimda qayta ishlamaydi.

### API endpointlari va rollar

`allone` — token bilan kirgan barcha rollar. Buyruq endpointlarida har bir amalning o'z ruxsati
tekshiriladi; agentga tegishli hisobot va ma'lumotlar agent doirasida filtrlanadi.

| Method | Endpoint | Ruxsat |
|---|---|---|
| `GET` | `/health` | Ochiq, token talab qilinmaydi |
| `POST` | `/api/auth/login` | Ochiq, token talab qilinmaydi |
| `POST` | `/api/auth/switch-task` | `allone` — faqat foydalanuvchiga biriktirilgan vazifaga |
| `POST` | `/api/commands` | `allone` — buyruq turi bo'yicha ruxsat tekshiriladi |
| `POST` | `/api/commands/batch` | `allone` — har bir buyruq turi bo'yicha ruxsat tekshiriladi |
| `GET` | `/api/reference/snapshot` | `allone` — agent ma'lumotlari o'z doirasida |
| `GET` | `/api/reports/sales` | `owner`, `accountant`, `agent` |
| `GET` | `/api/reports/debts` | `owner`, `accountant`, `agent` |
| `GET` | `/api/reports/inventory` | `owner`, `warehouse`, `agent` |
| `GET` | `/api/reports/expiring` | `owner`, `warehouse` |
| `GET` | `/api/reports/audit` | `owner` |

Telegram egasiga xabar yetkazish uchun `.env` dagi bot tokeni va egasining chat ID sini to'ldirib:

    docker compose --env-file infrastructure/env/.env -f infrastructure/docker/docker-compose.yml --profile notifications up -d outbox-relay telegram-bot

Relay va bot alohida jarayonlar. `/start` faqat sozlangan egasining chatida tasdiq xabarini qaytaradi;
boshqa Telegram buyruqlari hozircha qo'llanmaydi. Xabarlar avval PostgreSQL outbox'iga saqlanadi, Redis
oqim orqali tez uzatadi; yetkazish kamida bir marta bo'lgani sababli, Telegram yoki DB uzilishidan so'ng
xabar takror kelishi mumkin.

Tungi tekshiruvni tashqi cron yoki Windows Task Scheduler'dan har kuni ishga tushiring:

    docker compose --env-file infrastructure/env/.env -f infrastructure/docker/docker-compose.yml --profile jobs run --rm nightly

Tekshiruv hodisalar zanjiri, qoldiq/hujjat proyeksiyasi, muddati o'tgan qarz, yaroqlilik muddati o'tgan mahsulot,
kechikkan bahs va qabul qilinmagan qaytimlarni tekshiradi. U ma'lumotni avtomatik tuzatmaydi; muammo bo'lsa
egasiga outbox xabari qo'shadi va nol bo'lmagan kod qaytaradi.

Xaridda xom ashyo `weight_g` (butun gramm) va `unit_price_per_kg` (so'm/kg) bilan,
qadoq materiali dona va `unit_price` bilan kiritiladi. `payment_type` qiymatlari `cash`, `card`
yoki `credit`; qisman to'lovda to'lanmagan qismi yetkazib beruvchi qarziga yoziladi. Xom ashyo
xaridida `paid_amount` majburiy, qadoq xaridida esa berilmasa naqd/karta to'liq, qarz 0 so'm
to'langan deb olinadi. `supplier.pay` shu qarzni qisman yoki to'liq yopadi. Naqd to'lov kassa
chiqimi, karta to'lovi esa bank ko'chirmasi bilan solishtiriladigan chiqim bo'ladi. Xom ashyo
xaridining `road_cost` qiymati tannarxga qo'shiladi, alohida naqd chiqim hisoblanadi va
yetkazib beruvchi qarziga kirmaydi.

`sale.create` narxni serverdagi joriy narxdan oladi va uni sotuv qatorlariga nusxalaydi. Agent faqat
o'ziga biriktirilgan do'kon va zimmasidagi yaroqli partiyadan sotadi; chegirma uchun do'kon/xodimga
bog'langan bir martalik `discount` ruxsati kerak. `payment_type` — `cash`, `card` yoki `credit`;
naqd/karta to'lovi ko'rsatilmasa qolgan summa to'liq to'langan deb olinadi, `credit` esa joriy to'lovni
0 qiladi. Do'kon avansi avval ishlatiladi; qolgan qarz umumiy siyosat limitidan oshsa bir martalik
`credit_exception` ruxsati talab qilinadi. Naqd agent tushumi `agentcash`, karta tushumi
`bank:unmatched` ga yoziladi.

`payment.receive` tushumni eng eski ochiq sotuv qarzlaridan boshlab yopadi; qarzdan ortiq summa
do'kon avansiga aylanadi. `term.extend` egasining onlayn buyrug'i bilan ochiq sotuv muddatini
ko'rsatilgan kalendar kunlariga uzaytiradi. `dispute.open` ochiq qarz bo'yicha ish kunlari bilan
hal qilish muddatini qayd etadi; `dispute.resolve` qaror va izohni audit qiladi, ammo o'zi qarzni
o'zgartirmaydi. Moliyaviy tuzatish alohida tasdiqlangan amal bo'lishi kerak.

`return.declare` qaytimni sotuv va partiyaga bog'laydi, qaytgan mahsulotni agentdagi `hold` ga
qo'yadi va qaytim qiymatini sotuvdagi narx/chegirma nusxasidan hisoblaydi. Omborchi `return.accept`
da uni sotiladigan omborga (`restock`) yoki karantinga (`quarantine`) o'tkazadi; muddati o'tgan
mahsulotni sotiladigan qoldiqqa qaytarib bo'lmaydi. Egasi `return.settle` buyrug'i bilan avval shu
sotuvning ochiq qarzini kamaytiradi, ortgan summani esa do'kon avansiga yozadi; naqd pul avtomatik
qaytarilmaydi. `exchange.approve` shu kreditdan foydalanib agent zimmasidagi FIFO qoldiqdan yangi
sotuv yaratadi va yangi narxni nusxalaydi. `writeoff.approve` faqat karantin qoldig'ini kamaytiradi;
tannarx hujjatda saqlanadi. `transfer.prepare.custody_return` agentning sotilmagan qoldig'ini
omborga qaytarishni tayyorlaydi, `transfer.accept.custody_return` esa buni boshqa xodim tasdiqlashini talab qiladi.

`closing.submit` kuniga bir marta agentning to'rtta holatini (naqd pul, zimmasidagi tovar, unga
biriktirilgan do'konlar qarzi/avansi va kutilayotgan qaytim) snapshot qiladi hamda naqd pulni
sanab solishtiradi. Farq bo'lsa izoh talab qilinadi va egasiga xabar yuboriladi; balansdagi farq
avtomatik tuzatilmaydi. Hisobchi `transfer.accept.cash_handover` orqali ikki tomonlama tasdiqlab,
naqdni agent balansidan kompaniya kassasiga o'tkazadi.

`packaging.post` uchun `input_g`, `{product_id, quantity}` qatorlari, ishchilar (`workers`) va jami
yoki donalik ish haqi kerak. Partiya mahsulotlari bitta xom ashyodan bo'lishi shart; tizim BOM bo'yicha
qadoq materiallarini hisobdan chiqaradi, joriy o'rtacha tannarxni ishlatadi va FIFO tartibida xom ashyo
xarid manbalarini qayd etadi. Qadoqlovchi amal qiladigan bir martalik `labor` ruxsatini kiritadi.
Kam chiqish statistikasi faqat siyosatdagi partiya/og'irlik chegaralaridan so'ng ko'rsatiladi.
Abnormal kam chiqishni tannarxdan ajratish v2 formulasi taqdim etilmagani sabab hozircha qo'llanmaydi;
bu bosqichda barcha o'lchangan kam chiqish yoziladi va xom ashyo qiymati tayyor mahsulotlarga taqsimlanadi.

Yangi buyruq qo'shish: `@command("sale.create")` bilan handler yozing, `ctx.emit(...)` bilan hodisa yozing,
`@projects("sale_recorded")` bilan uning qoldiqqa ta'sirini bering, amalni `permissions.json` ga qo'shing.
Yangi jadval kerak bo'lsa: SQLite (`schema.py`) VA PostgreSQL (`migrations/postgres/`) migratsiyasini birga yozing.
