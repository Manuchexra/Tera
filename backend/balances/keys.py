"""Qoldiq kalitlari yagona joyda. Format: `tur:id[:id...]`. ID larda ':' bo'lmaydi.

Birliklar: xom ashyo — gramm, qadoq materiali va tayyor mahsulot — dona, pul — so'm.
"""

CASH = "cash:company"                  # kompaniya kassasi (tasdiqlangan naqd)
BANK = "bank:company"                  # bank hisobi (tasdiqlangan)
BANK_UNMATCHED = "bank:unmatched"      # karta/o'tkazma deb aytilgan, bank ko'chirmasi bilan hali tekshirilmagan


def raw(material_id): return f"raw:{material_id}"                 # xom ashyo, gramm
def raw_value(material_id): return f"rawval:{material_id}"        # xom ashyo qiymati, so'm (o'rtacha tannarx uchun)
def pack(material_id): return f"pack:{material_id}"               # qadoq materiali, dona
def pack_value(material_id): return f"packval:{material_id}"      # qadoq materiali qiymati, so'm

def fg(product_id, batch_id): return f"fg:{product_id}:{batch_id}"                # tayyor mahsulot, omborda
def custody(user_id, product_id, batch_id): return f"custody:{user_id}:{product_id}:{batch_id}"   # agent zimmasi, sotsa bo'ladi
def hold(user_id, product_id, batch_id): return f"hold:{user_id}:{product_id}:{batch_id}"         # agentdagi qaytim, sotilmaydi
def quarantine(product_id, batch_id): return f"quarantine:{product_id}:{batch_id}"                # tekshiruvdagi/yaroqsiz

def agent_cash(user_id): return f"agentcash:{user_id}"            # agent qo'lidagi naqd
def debt(store_id): return f"debt:{store_id}"                     # do'kon jami ochiq qarzi
def advance(store_id): return f"advance:{store_id}"               # do'kon oldindan to'lagan summa
def supplier_debt(supplier_id): return f"supplierdebt:{supplier_id}"

def stat(name, period): return f"stat:{name}:{period}"            # oylik hisoblagichlar (xarajat, kassa farqi...)
def loss_stat(name, material_id): return f"loss:{name}:{material_id}"   # kam chiqish statistikasi (in/out/n)


def prefix(*parts) -> str:
    """Prefiks bo'yicha qidirish uchun: prefix('custody', user) -> 'custody:user:'"""
    return ":".join(parts) + ":"
