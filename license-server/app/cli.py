from __future__ import annotations

import argparse
import os

from sqlalchemy import select

from .bootstrap import seed_defaults
from .db import SessionLocal, init_db
from .models import AdminUser, new_id
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


def main() -> None:
    parser = argparse.ArgumentParser(description="X5Agent license server CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="create or update an admin")
    create.add_argument("--email", required=True)
    # A password on argv is visible in the process list: prefer ADMIN_PASSWORD.
    create.add_argument("--password")

    sub.add_parser("seed", help="create defaults (trial plan, bootstrap admin)")

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


if __name__ == "__main__":
    main()
