"""Jarayon modullari. Har bir modul o'z projeksiyalari va buyruqlarini import paytida ro'yxatdan o'tkazadi."""


def load_all() -> None:
    """Barcha modullarni yuklaydi: hodisa projeksiyalari (jurnalni qayta qurish uchun) va buyruqlar (dvigatel uchun)."""
    from backend.modules.approvals import handlers as _ap, projections as _app  # noqa: F401
    from backend.modules.auth import projections as _a  # noqa: F401
    from backend.modules.closing import handlers as _cl, projections as _clp  # noqa: F401
    from backend.modules.policy import handlers as _po, projections as _pop  # noqa: F401
    from backend.modules.packaging import handlers as _pa, projections as _pap  # noqa: F401
    from backend.modules.pricing import handlers as _pr, projections as _prp  # noqa: F401
    from backend.modules.purchase import handlers as _pu, projections as _pup  # noqa: F401
    from backend.modules.reference import handlers as _r, projections as _rp  # noqa: F401
    from backend.modules.returns import handlers as _re, projections as _rep  # noqa: F401
    from backend.modules.sales import handlers as _sa, projections as _sap  # noqa: F401
    from backend.modules.warehouse import handlers as _w, projections as _wp  # noqa: F401
    from backend.modules.sync import projections as _s  # noqa: F401
