"""提供单管理员账户初始化与密码重置命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import getpass

from sqlalchemy import select

from devops.config import get_settings
from devops.db import Database
from devops.domain.models import AdminUser
from devops.security import hash_password


async def init_admin(username: str | None, password: str | None) -> None:
    """创建或更新本地开发环境的首个管理员账户。"""
    settings = get_settings()
    resolved_username = username or settings.admin_username
    resolved_password = password or settings.admin_initial_password or getpass.getpass("Password: ")
    if len(resolved_password) < 12:
        raise SystemExit("Administrator password must contain at least 12 characters")
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        async with database.session_factory() as session:
            existing = await session.scalar(
                select(AdminUser).where(AdminUser.username == resolved_username)
            )
            if existing:
                existing.password_hash = hash_password(resolved_password)
                existing.is_active = True
                action = "updated"
            else:
                session.add(
                    AdminUser(
                        username=resolved_username,
                        password_hash=hash_password(resolved_password),
                    )
                )
                action = "created"
            await session.commit()
        print(f"Administrator {resolved_username!r} {action}.")
    finally:
        await database.dispose()


def main() -> None:
    """运行命令行子命令入口。"""
    parser = argparse.ArgumentParser(prog="python -m devops.cli")
    # 注册子命令；required=True 保证无参数调用时直接报错退出。
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-admin", help="create or reset the single administrator")
    init.add_argument("--username", help="管理员用户名，省略则读 .env 中的 DEVOPS_ADMIN_USERNAME")
    init.add_argument("--password",help="管理员密码，至少 12 字符；省略则读 .env 中的 DEVOPS_ADMIN_INITIAL_PASSWORD",)
    args = parser.parse_args()
    if args.command == "init-admin":
        asyncio.run(init_admin(args.username, args.password))


if __name__ == "__main__":
    main()
