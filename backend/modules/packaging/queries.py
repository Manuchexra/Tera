"""Partiyalar va kam chiqish statistikasi."""
from decimal import Decimal, ROUND_HALF_UP


def _rate(shrink_g: int, input_g: int) -> Decimal:
    return Decimal(shrink_g) * 100 / Decimal(input_g)


def _format_pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _different_by_more_than(value: Decimal, average: Decimal, threshold_pct: int) -> bool:
    if average == 0:
        return value > 0
    return abs(value - average) * 100 > average * threshold_pct


def get_batch(docs, batch_id: str) -> dict | None:
    doc = docs.get(f"batch:{batch_id}")
    return dict(doc.data) if doc else None


def list_batches(docs, material_id: str | None = None, product_id: str | None = None) -> list[dict]:
    batches = docs.query("batch", ref=material_id)
    return [dict(doc.data) for doc in batches
            if product_id is None or any(line["product_id"] == product_id for line in doc.data["lines"])]


def expiring_batches(docs, from_date: str, through_date: str,
                     material_id: str | None = None) -> list[dict]:
    """Berilgan sana oralig'ida yaroqlilik muddati tugaydigan partiyalar."""
    return sorted(
        (batch for batch in list_batches(docs, material_id)
         if from_date <= batch["expires_on"] <= through_date),
        key=lambda batch: (batch["expires_on"], batch["batch_id"]),
    )


def shrink_summary(docs, material_id: str, policy) -> dict:
    batches = docs.query("batch", ref=material_id, status="posted")
    total_input_g = sum(doc.data["input_g"] for doc in batches)
    total_shrink_g = sum(doc.data["shrink_g"] for doc in batches)
    enough_data = (len(batches) >= policy.shrink_min_batches
                   and total_input_g >= policy.shrink_min_input_g)
    result = {
        "status": "ready" if enough_data else "collecting",
        "batch_count": len(batches),
        "total_input_g": total_input_g,
        "total_shrink_g": total_shrink_g,
        "weighted_average_pct": None,
        "median_pct": None,
        "min_pct": None,
        "max_pct": None,
        "outliers": [],
    }
    if not enough_data:
        result["message"] = "Ma'lumot yig'ilmoqda"
        return result

    rates = [(doc.data["batch_id"], _rate(doc.data["shrink_g"], doc.data["input_g"]))
             for doc in batches]
    average = _rate(total_shrink_g, total_input_g)
    ordered = sorted(rate for _, rate in rates)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    result.update({
        "weighted_average_pct": _format_pct(average),
        "median_pct": _format_pct(median),
        "min_pct": _format_pct(ordered[0]),
        "max_pct": _format_pct(ordered[-1]),
        "outliers": [
            {"batch_id": batch_id, "shrink_pct": _format_pct(rate)}
            for batch_id, rate in rates
            if _different_by_more_than(rate, average, policy.shrink_deviation_pct)
        ],
    })
    return result


def is_shrink_outlier(shrink_g: int, input_g: int, historical: dict, threshold_pct: int) -> bool:
    if historical["status"] != "ready":
        return False
    average = _rate(historical["total_shrink_g"], historical["total_input_g"])
    return _different_by_more_than(_rate(shrink_g, input_g), average, threshold_pct)
