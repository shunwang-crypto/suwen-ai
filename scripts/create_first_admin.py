#!/usr/bin/env python3
"""Create the first platform administrator through a one-time interactive flow."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from decimal import Decimal
from urllib.parse import urlsplit

from eduagents_api.models import User, WalletAccount, WalletLedger
from eduagents_api.security import hash_password
from eduagents_core.config import get_settings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--confirm-instance", required=True)
    parser.add_argument("--instance-name", default=os.environ.get("PURGE_INSTANCE_NAME", "eduagents-prod"))
    parser.add_argument("--expected-db-host", default=os.environ.get("PURGE_EXPECTED_DB_HOST"))
    parser.add_argument("--expected-database", default=os.environ.get("PURGE_EXPECTED_DATABASE", "eduagents_prod"))
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    if settings.app_env != "prod":
        raise RuntimeError("first-admin flow requires APP_ENV=prod")
    if args.confirm_instance != args.instance_name:
        raise RuntimeError("--confirm-instance does not match the configured instance")
    if not args.expected_db_host:
        raise RuntimeError("PURGE_EXPECTED_DB_HOST or --expected-db-host is required")

    parsed = urlsplit(settings.migration_database_url)
    database = (parsed.path or "").lstrip("/")
    if parsed.hostname != args.expected_db_host or database != args.expected_database:
        raise RuntimeError(f"unexpected migration target: {parsed.hostname}:{parsed.port}/{database}")

    password = getpass.getpass("New admin password: ")
    repeat = getpass.getpass("Repeat admin password: ")
    if password != repeat:
        raise RuntimeError("passwords do not match")
    if len(password) < 8:
        raise RuntimeError("password must contain at least 8 characters")

    engine = create_async_engine(settings.migration_database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            async with session.begin():
                if await session.scalar(select(func.count()).select_from(User).where(User.role == "admin")):
                    raise RuntimeError("an administrator already exists; refusing to create another first admin")
                if await session.scalar(select(User).where(User.email == args.email)):
                    raise RuntimeError("email already exists")
                user = User(
                    email=args.email,
                    display_name="平台管理员",
                    password_hash=hash_password(password, settings.bcrypt_rounds),
                    role="admin",
                )
                session.add(user)
                await session.flush()
                grant_amount = Decimal(str(settings.signup_grant_cny))
                grant = WalletAccount(user_id=user.id, balance=grant_amount)
                session.add(grant)
                session.add(
                    WalletLedger(
                        user_id=user.id,
                        delta=grant_amount,
                        balance_after=grant_amount,
                        kind="signup_grant",
                        note="first-admin bootstrap grant",
                    )
                )
        print(f"FIRST_ADMIN_CREATED email={args.email} user_id={user.id}")
    finally:
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except Exception as exc:  # noqa: BLE001 - CLI reports a clear failed bootstrap
        raise SystemExit(f"FIRST_ADMIN_ABORTED={type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    main()
