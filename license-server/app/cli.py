from __future__ import annotations

import argparse
import os

from sqlalchemy import select

from .bootstrap import seed_defaults
from .db import SessionLocal, init_db
from .models import Account, AdminUser, new_id
from .security import hash_password
from .validation import EMAIL_RE


def create_admin(email: str, password: str) -> None:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise SystemExit("invalid admin email")
    if len(password) < 8:
        raise SystemExit("admin password must be at least 8 characters")

    init_db()
    with SessionLocal() as db:
        admin = db.scalars(
            select(AdminUser).where(AdminUser.email == email)
        ).first()
        if admin is None:
            db.add(
                AdminUser(
                    id=new_id(),
                    email=email,
                    password_hash=hash_password(password),
                    is_active=True,
                )
            )
            action = "created"
        else:
            admin.password_hash = hash_password(password)
            admin.is_active = True
            action = "updated"
        db.commit()
    print(f"admin {action}: {email}")


def reset_account_password(email: str, password: str) -> None:
    """Set a customer account password (operator recovery, never printed).

    The console owner and the license account share one email/password. When an
    account was created earlier with a different password, the client's sign-in
    can never attach it (the server refuses a duplicate email), so the operator
    resets it here instead of deleting paid data.
    """
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise SystemExit("invalid account email")
    if len(password) < 8:
        raise SystemExit("account password must be at least 8 characters")

    init_db()
    with SessionLocal() as db:
        account = db.scalars(select(Account).where(Account.email == email)).first()
        if account is None:
            raise SystemExit("no account for that email")
        account.password_hash = hash_password(password)
        db.commit()
    print(f"account password reset: {email}")


def main() -> None:
    parser = argparse.ArgumentParser(description="X5Agent license server CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="create or update an admin")
    create.add_argument("--email", required=True)
    # A password on argv is visible in the process list: prefer ADMIN_PASSWORD.
    create.add_argument("--password")

    sub.add_parser("seed", help="create defaults (trial plan, bootstrap admin)")

    reset = sub.add_parser(
        "reset-password", help="set a customer account password (recovery)"
    )
    reset.add_argument("--email", required=True)
    # Same rule as create-admin: a password on argv leaks into the process list.
    reset.add_argument("--password")

    args = parser.parse_args()

    if args.command == "create-admin":
        password = args.password or os.environ.get("ADMIN_PASSWORD", "")
        if not password:
            raise SystemExit("admin password required (--password or ADMIN_PASSWORD)")
        create_admin(args.email, password)
    elif args.command == "seed":
        init_db()
        seed_defaults()
        print("defaults seeded")
    elif args.command == "reset-password":
        password = args.password or os.environ.get("ACCOUNT_PASSWORD", "")
        if not password:
            raise SystemExit(
                "account password required (--password or ACCOUNT_PASSWORD)"
            )
        reset_account_password(args.email, password)


if __name__ == "__main__":
    main()
