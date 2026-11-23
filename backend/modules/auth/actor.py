from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    """Buyruqni kim, qaysi vazifada, qaysi qurilmadan yuboryapti."""
    user_id: str
    task: str
    device_id: str
