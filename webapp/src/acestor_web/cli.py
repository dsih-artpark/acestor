"""CLI for acestor_web admin operations.

Usage: python -m acestor_web.cli <command> [args]
"""

import argparse
import getpass
import sys
import uuid
from datetime import UTC, datetime

from acestor_web.audit import write_audit
from acestor_web.auth.passwords import WeakPasswordError, check_policy, hash_password
from acestor_web.db import SessionLocal
from acestor_web.models import User
from acestor_web.models.user import AuthProvider


def _get_user_by_email(session, email: str) -> User | None:
    return session.query(User).filter_by(email=email.lower()).one_or_none()


def cmd_users_create(args: argparse.Namespace) -> int:
    password = args.password
    if password is None and args.prompt_password:
        password = getpass.getpass("Password: ")

    if not password:
        print(
            "error: password is required (--password or --prompt-password)",
            file=sys.stderr,
        )
        return 1

    try:
        check_policy(password)
    except WeakPasswordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    session = SessionLocal()
    try:
        existing = _get_user_by_email(session, args.email)
        if existing:
            print(f"error: email already exists: {args.email}", file=sys.stderr)
            return 1

        user = User(
            id=uuid.uuid4(),
            email=args.email.lower(),
            name=args.name or "",
            auth_provider=AuthProvider.local,
            password_hash=hash_password(password),
            is_admin=args.admin,
            is_active=True,
        )
        session.add(user)
        session.flush()
        write_audit(
            session,
            user_id=None,
            action="user.create",
            target_type="user",
            target_id=user.id,
            after={
                "email": user.email,
                "name": user.name,
                "auth_provider": "local",
                "is_admin": user.is_admin,
                "is_active": user.is_active,
            },
        )
        session.commit()
        print(str(user.id))
        return 0
    except Exception as exc:
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def cmd_users_list(args: argparse.Namespace) -> int:
    session = SessionLocal()
    try:
        users = session.query(User).order_by(User.email).all()
        cols = ("id", "email", "auth_provider", "is_admin", "is_active")
        header = (
            f"{cols[0]:<36}  {cols[1]:<40}  {cols[2]:<15}  {cols[3]:<8}  {cols[4]:<9}"
        )
        print(header)
        print("-" * len(header))
        for u in users:
            provider = (
                u.auth_provider.value
                if hasattr(u.auth_provider, "value")
                else str(u.auth_provider)
            )
            row = (
                f"{str(u.id):<36}  {u.email:<40}  "
                f"{provider:<15}  {str(u.is_admin):<8}  {str(u.is_active):<9}"
            )
            print(row)
        return 0
    finally:
        session.close()


def cmd_users_disable(args: argparse.Namespace) -> int:
    session = SessionLocal()
    try:
        user = _get_user_by_email(session, args.email)
        if user is None:
            print(f"error: user not found: {args.email}", file=sys.stderr)
            return 1
        if not user.is_active:
            print(f"user already inactive: {args.email}")
            return 0
        user.is_active = False
        session.flush()
        write_audit(
            session,
            user_id=None,
            action="user.disable",
            target_type="user",
            target_id=user.id,
            before={"is_active": True},
            after={"is_active": False},
        )
        session.commit()
        print(f"disabled: {args.email}")
        return 0
    except Exception as exc:
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def cmd_users_enable(args: argparse.Namespace) -> int:
    session = SessionLocal()
    try:
        user = _get_user_by_email(session, args.email)
        if user is None:
            print(f"error: user not found: {args.email}", file=sys.stderr)
            return 1
        if user.is_active:
            print(f"user already active: {args.email}")
            return 0
        user.is_active = True
        session.flush()
        write_audit(
            session,
            user_id=None,
            action="user.enable",
            target_type="user",
            target_id=user.id,
            before={"is_active": False},
            after={"is_active": True},
        )
        session.commit()
        print(f"enabled: {args.email}")
        return 0
    except Exception as exc:
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def cmd_users_set_password(args: argparse.Namespace) -> int:
    password = args.password
    if password is None and args.prompt_password:
        password = getpass.getpass("New password: ")

    if not password:
        print(
            "error: password is required (--password or --prompt-password)",
            file=sys.stderr,
        )
        return 1

    try:
        check_policy(password)
    except WeakPasswordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    session = SessionLocal()
    try:
        user = _get_user_by_email(session, args.email)
        if user is None:
            print(f"error: user not found: {args.email}", file=sys.stderr)
            return 1

        provider = (
            user.auth_provider.value
            if hasattr(user.auth_provider, "value")
            else str(user.auth_provider)
        )
        if provider != "local":
            print(
                f"error: not applicable to this provider: {provider}", file=sys.stderr
            )
            return 1

        user.password_hash = hash_password(password)
        user.password_changed_at = datetime.now(tz=UTC)
        session.flush()
        write_audit(
            session,
            user_id=None,
            action="user.reset_password",
            target_type="user",
            target_id=user.id,
            before={},
            after={},
        )
        session.commit()
        print(f"password updated: {args.email}")
        return 0
    except Exception as exc:
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acestor_web.cli",
        description="acestor_web admin CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # users subcommand
    users_parser = sub.add_parser("users", help="user management")
    users_sub = users_parser.add_subparsers(dest="users_command", required=True)

    # users create
    create_p = users_sub.add_parser("create", help="create a local user")
    create_p.add_argument("email", help="user email")
    create_p.add_argument(
        "--admin", action="store_true", default=False, help="grant admin"
    )
    create_p.add_argument("--name", default="", help="display name")
    pw_group = create_p.add_mutually_exclusive_group()
    pw_group.add_argument("--password", default=None, help="password (plaintext)")
    pw_group.add_argument(
        "--prompt-password",
        action="store_true",
        default=False,
        help="prompt for password via stdin",
    )

    # users list
    users_sub.add_parser("list", help="list all users")

    # users disable
    disable_p = users_sub.add_parser("disable", help="soft-delete a user")
    disable_p.add_argument("email", help="user email")

    # users enable
    enable_p = users_sub.add_parser("enable", help="re-activate a user")
    enable_p.add_argument("email", help="user email")

    # users set-password
    setpw_p = users_sub.add_parser("set-password", help="reset a local user's password")
    setpw_p.add_argument("email", help="user email")
    setpw_pw_group = setpw_p.add_mutually_exclusive_group()
    setpw_pw_group.add_argument(
        "--password", default=None, help="new password (plaintext)"
    )
    setpw_pw_group.add_argument(
        "--prompt-password",
        action="store_true",
        default=False,
        help="prompt for new password via stdin",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "users":
        cmd = args.users_command
        handlers = {
            "create": cmd_users_create,
            "list": cmd_users_list,
            "disable": cmd_users_disable,
            "enable": cmd_users_enable,
            "set-password": cmd_users_set_password,
        }
        handler = handlers.get(cmd)
        if handler is None:
            parser.print_help()
            sys.exit(1)
        sys.exit(handler(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
