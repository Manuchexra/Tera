"""Xizmatni boshqarish uchun xavfsiz bir martalik buyruqlar."""
import argparse
from getpass import getpass

from backend.core.clock import SystemClock
from backend.core.config import Settings
from backend.ledger.db import DatabaseProvider
from backend.ledger.journal import Journal
from backend.modules.auth.service import AuthService
from backend.modules.auth.tokens import TokenService


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.cli")
    subparsers = parser.add_subparsers(dest="action", required=True)
    bootstrap = subparsers.add_parser("bootstrap-owner", help="Birinchi egani yaratish")
    bootstrap.add_argument("--login", required=True)
    bootstrap.add_argument("--full-name", required=True)
    bootstrap.add_argument("--phone")
    args = parser.parse_args()

    settings = Settings.from_env()
    password = getpass("Yangi egasining paroli: ")
    provider = DatabaseProvider(settings.database_url, settings.pool_min, settings.pool_max)
    try:
        with provider.session() as db:
            clock = SystemClock()
            service = AuthService(
                Journal(db, clock),
                TokenService(settings.secret, clock, settings.token_ttl_seconds),
            )
            user = service.bootstrap_owner(args.login, password, args.full_name, args.phone)
        print(f"Ega yaratildi: {user.user_id} ({user.login})")
    finally:
        provider.close()


if __name__ == "__main__":
    main()
