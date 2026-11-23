"""Pul va og'irlik.

Pul: butun so'm. Og'irlik: butun gramm. Dona: butun son.
Kasr faqat hisoblash paytida (Decimal), natija har doim butun songa yaxlitlanadi (0,5 yuqoriga).
"""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


def round_uzs(value) -> int:
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def pct(numerator, denominator, places: int = 2) -> Decimal:
    """Foiz: numerator / denominator * 100, `places` xona aniqlikda. Maxraj 0 bo'lsa 0."""
    if not denominator:
        return Decimal(0)
    q = Decimal(1).scaleb(-places)
    return (Decimal(numerator) * 100 / Decimal(denominator)).quantize(q, rounding=ROUND_HALF_UP)


def allocate_proportionally(total: int, weights: list[int]) -> list[int]:
    """`total` ni `weights` nisbatida butun sonlarga bo'ladi (eng katta qoldiq usuli).

    Yig'indi aniq `total` ga teng. Masalan chegirmani sotuv qatorlariga taqsimlashda ishlatiladi.
    """
    if not weights:
        return []
    if total == 0:
        return [0] * len(weights)
    sw = sum(weights)
    if sw <= 0:
        raise ValueError("weights sum must be positive")
    raw = [Decimal(total) * w / sw for w in weights]
    floors = [int(r.to_integral_value(rounding=ROUND_DOWN)) for r in raw]
    rest = total - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: (raw[i] - floors[i], -i), reverse=True)
    for i in order[:rest]:
        floors[i] += 1
    return floors
