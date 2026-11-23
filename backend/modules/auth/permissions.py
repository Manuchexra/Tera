"""Ruxsatlar jadvali (talablar §3.3). Qiymatlar `shared/constants/permissions.json` da, mobil va bot bilan umumiy.

Ruxsat vazifaga (task) beriladi, odamga emas: bir odam bugun agent, ertaga omborchi bo'lishi mumkin (§3.1).
Jadvalda bo'lmagan amal hammaga taqiqlangan.
"""
import json
from pathlib import Path

PERMISSIONS_JSON = Path(__file__).resolve().parents[3] / "shared" / "constants" / "permissions.json"


class Permissions:
    def __init__(self, tasks: list[str], actions: dict[str, list[str]]):
        self.tasks = list(tasks)
        self._actions = {a: set(t) for a, t in actions.items()}
        for action, ts in self._actions.items():
            bad = ts - set(self.tasks)
            if bad:
                raise ValueError(f"{action}: noma'lum vazifa {sorted(bad)}")

    @classmethod
    def default(cls) -> "Permissions":
        data = json.loads(PERMISSIONS_JSON.read_text(encoding="utf-8"))
        return cls(data["tasks"], data["actions"])

    def allowed(self, task: str, action: str) -> bool:
        return task in self._actions.get(action, ())

    def tasks_for(self, action: str) -> list[str]:
        return sorted(self._actions.get(action, ()))

    def extend(self, actions: dict[str, list[str]]) -> None:
        """Yangi amallarni qo'shadi (testlar va keyingi modullar uchun)."""
        for action, ts in actions.items():
            bad = set(ts) - set(self.tasks)
            if bad:
                raise ValueError(f"{action}: noma'lum vazifa {sorted(bad)}")
            self._actions.setdefault(action, set()).update(ts)

    def is_task(self, name: str) -> bool:
        return name in self.tasks
