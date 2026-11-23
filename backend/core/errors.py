"""Domen xatolari. Har bir xato mashina o'qiydigan `code` ga ega: mobil ilova shu bo'yicha ekran ko'rsatadi."""


class DomainError(Exception):
    """Qoida buzildi: buyruq rad etiladi, jurnalga hech narsa yozilmaydi."""

    status = "rejected"

    def __init__(self, code: str, message: str = "", **details):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.details = details


class Forbidden(DomainError):
    """Bu vazifa (rol) uchun ruxsat yo'q."""


class Conflict(DomainError):
    """Buyruq to'g'ri, lekin hozirgi holatga sig'madi (masalan, qarz limiti oshdi).

    Asl yozuv `sync_inbox` da saqlanadi va egasi ko'rib chiqadi (System 04 ekrani).
    "Oxirgi yozgan yutadi" qoidasi hech qachon ishlatilmaydi.
    """

    status = "conflict"
