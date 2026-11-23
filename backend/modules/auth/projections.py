"""Kirish hodisalari faqat tarix uchun: qoldiqqa ta'sir qilmaydi, lekin "kim qachon nima qildi" jurnalda qoladi."""
from backend.balances.projection import no_effect

no_effect("user_created", "user_updated", "password_changed",
          "session_started", "session_task_switched", "device_registered", "device_revoked")
