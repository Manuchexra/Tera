"""Buyruqlar registri. Har bir jarayon (sotuv, xarid, ...) o'z buyruqlarini shu yerda ro'yxatdan o'tkazadi.

    @command("sale.create")
    def create_sale(ctx, payload): ...

Buyruq nomi ruxsatlar jadvalidagi amal nomi bilan bir xil bo'ladi (aniqroq tekshiruv kerak bo'lsa, ctx.require(...)).
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Command:
    """Mobil ilova yuboradigan buyruq. `event_id` qurilmada yaratiladi va takroriy yuborishdan himoya qiladi."""
    event_id: str
    type: str
    payload: dict
    device_time: str
    est_server_time: str | None = None
    gps: dict | None = None

    def to_json(self) -> dict:
        return {"event_id": self.event_id, "type": self.type, "payload": self.payload,
                "device_time": self.device_time, "est_server_time": self.est_server_time, "gps": self.gps}


@dataclass(frozen=True)
class CommandSpec:
    name: str
    action: str
    handler: Callable


_COMMANDS: dict[str, CommandSpec] = {}


def command(name: str, action: str | None = None):
    def deco(fn: Callable) -> Callable:
        if name in _COMMANDS:
            raise ValueError(f"{name} buyrug'i allaqachon ro'yxatdan o'tgan")
        _COMMANDS[name] = CommandSpec(name, action or name, fn)
        return fn
    return deco


def get_command(name: str) -> CommandSpec | None:
    return _COMMANDS.get(name)


def registered_commands() -> list[str]:
    return sorted(_COMMANDS)
