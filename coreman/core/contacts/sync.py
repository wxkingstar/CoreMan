"""通讯录归并。一次 apply = 一个事务。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from coreman.core.contacts.types import Directory, DirectoryUser
from coreman.core.db.models import Department, TeamRule, User, UserDepartment, UserIdentity
from coreman.core.logging import get_logger

log = get_logger(__name__)
DISABLE_LIMIT = 100


async def _team_rules(session: AsyncSession) -> list[TeamRule]:
    stmt = select(TeamRule).order_by(TeamRule.sort_order, TeamRule.created_at)
    return list((await session.execute(stmt)).scalars().all())


class SyncAborted(Exception):
    """一次同步停用人数超过 DISABLE_LIMIT，已回滚。"""


@dataclass
class SyncStats:
    """一次 apply 的统计结果；to_dict() 只含 int 与 list[str]，可直接落 JSONB。"""

    departments: int = 0
    users_total: int = 0
    created: int = 0
    updated: int = 0
    disabled: int = 0
    reactivated: int = 0
    login_name_conflicts: list[str] = field(default_factory=list)
    field_conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _login_name_for(u: DirectoryUser) -> str:
    """login_name：邮箱前缀（parse_wecom_directory 已把 biz_mail 兜底进 email），否则用平台 userid。

    冲突（与他人 login_name 重复）由调用方处理：留空并记入 SyncStats.login_name_conflicts。
    """
    return u.email.split("@", 1)[0].lower() if u.email else u.platform_user_id


class ContactSyncService:
    """把抓取到的 Directory 归并进账号库；一次 apply 是一个数据库事务。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def apply(self, directory: Directory) -> SyncStats:
        """归并整份目录：先部门、再逐人归并、最后停用消失的人。

        停用人数超过 DISABLE_LIMIT 时整体回滚并抛 SyncAborted，避免一次误同步把全公司停用。

        Args:
            directory: 平台无关的通讯录目录

        Returns:
            本次同步的统计结果

        Raises:
            SyncAborted: 目录为空，或一次同步停用人数超过 DISABLE_LIMIT
        """
        if not directory.users:
            # 空目录几乎只可能是抓取异常（权限被收回、IP 不在白名单、接口变更），
            # 按「所有人都从目录里消失了」处理会静默停用全公司。直接中止，一行都不改。
            raise SyncAborted("目录为空，疑似抓取异常，已中止")
        stats = SyncStats(users_total=len(directory.users))
        async with self._factory() as session:
            # 两个平台同时同步时，跨平台归并也必须是一笔串行裁决。
            await session.execute(text("SELECT pg_advisory_xact_lock(582190401)"))
            dept_map = await self._sync_departments(session, directory)
            stats.departments = len(dept_map)
            rules = await _team_rules(session)
            seen: set[str] = set()
            for du in directory.users:
                seen.add(du.platform_user_id)
                await self._merge_user(session, directory.platform, du, dept_map, rules, stats)
            await self._disable_missing(session, directory.platform, seen, stats)
            if stats.disabled > DISABLE_LIMIT:
                await session.rollback()
                raise SyncAborted(
                    f"一次同步停用 {stats.disabled} 人，超过上限 {DISABLE_LIMIT}，已中止"
                )
            await session.commit()
        log.info(
            "contact_sync_applied",
            platform=directory.platform,
            **{k: v for k, v in stats.to_dict().items() if not isinstance(v, list)},
        )
        return stats

    async def merge_one(self, platform: str, du: DirectoryUser) -> SyncStats:
        """只归并一个成员（登录时查不到人的补同步）：不建部门、不停用任何人。

        部门关联只连到已经同步过的部门；新部门等下一次整份同步补齐。
        与整份同步共用同一把咨询锁，归并规则也完全一致。

        Raises:
            SyncAborted: 该成员的邮箱与手机号分别指向不同账号
        """
        stats = SyncStats(users_total=1)
        async with self._factory() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(582190401)"))
            dept_map = {
                d.platform_dept_id: d
                for d in (
                    await session.execute(select(Department).where(Department.platform == platform))
                ).scalars()
            }
            rules = await _team_rules(session)
            await self._merge_user(session, platform, du, dept_map, rules, stats)
            await session.commit()
        log.info(
            "contact_user_merged",
            platform=platform,
            user_id=du.platform_user_id,
            created=stats.created,
        )
        return stats

    async def _sync_departments(
        self, session: AsyncSession, directory: Directory
    ) -> dict[str, Department]:
        """按 platform_dept_id upsert 部门行，刷新 name/path/sort_order 与 parent_id。

        先保证所有行存在，再统一回填 parent_id：目录里父部门可能排在子部门之后。

        Returns:
            仅包含本次目录中出现的部门（platform_dept_id -> Department）
        """
        existing = {
            d.platform_dept_id: d
            for d in (
                await session.execute(
                    select(Department).where(Department.platform == directory.platform)
                )
            ).scalars()
        }
        by_id = {d.platform_dept_id: d for d in directory.departments}

        def path_of(pid: str) -> str:
            parts: list[str] = []
            cur: str | None = pid
            guard = 0
            while cur and cur in by_id and guard < 64:
                parts.append(by_id[cur].name)
                cur = by_id[cur].parent_platform_dept_id
                guard += 1
            return "/".join(reversed(parts))

        for dd in directory.departments:
            row = existing.get(dd.platform_dept_id)
            if row is None:
                row = Department(
                    platform=directory.platform,
                    platform_dept_id=dd.platform_dept_id,
                    name=dd.name,
                    path=path_of(dd.platform_dept_id),
                )
                session.add(row)
                existing[dd.platform_dept_id] = row
            row.name, row.path, row.sort_order = (
                dd.name,
                path_of(dd.platform_dept_id),
                dd.sort_order,
            )
        await session.flush()
        for dd in directory.departments:
            parent_pid = dd.parent_platform_dept_id
            parent = existing.get(parent_pid) if parent_pid else None
            existing[dd.platform_dept_id].parent_id = parent.id if parent else None
        await session.flush()
        return {k: v for k, v in existing.items() if k in by_id}

    async def _find_user(
        self, session: AsyncSession, platform: str, du: DirectoryUser
    ) -> tuple[User | None, UserIdentity | None]:
        """按归并规则查找已存在的用户：identity → lower(email) → mobile。

        邮箱/手机号归并只在 source='sync' 的行里找，不会误认到手工/引导账号上。
        邮箱比较必须是等值：ILIKE 会把目录邮箱当成 LIKE 模式，其中的 `_`/`%`
        会通配到别人的邮箱上，把两个人错误地归并成一行。
        """
        ident = (
            await session.execute(
                select(UserIdentity)
                .options(selectinload(UserIdentity.user))
                .where(
                    UserIdentity.platform == platform,
                    UserIdentity.platform_user_id == du.platform_user_id,
                )
            )
        ).scalar_one_or_none()
        if ident is not None:
            return ident.user, ident
        if du.email and du.mobile:
            candidates = list(
                await session.scalars(
                    select(User.id).where(
                        User.source == "sync",
                        (func.lower(User.email) == du.email.lower()) | (User.mobile == du.mobile),
                    )
                )
            )
            if len(set(candidates)) > 1:
                raise SyncAborted("邮箱与手机号指向不同账号，已中止归并")
        if du.email:
            u = (
                await session.execute(
                    select(User).where(
                        func.lower(User.email) == du.email.lower(), User.source == "sync"
                    )
                )
            ).scalar_one_or_none()
            if u is not None:
                return u, None
        if du.mobile:
            u = (
                await session.execute(
                    select(User).where(User.mobile == du.mobile, User.source == "sync")
                )
            ).scalar_one_or_none()
            if u is not None:
                return u, None
        return None, None

    async def _merge_user(
        self,
        session: AsyncSession,
        platform: str,
        du: DirectoryUser,
        dept_map: dict[str, Department],
        rules: list[TeamRule],
        stats: SyncStats,
    ) -> None:
        """归并单个目录成员：找人或建人、刷新字段、生成 login_name、重建部门关联、命中团队规则。"""
        user, ident = await self._find_user(session, platform, du)
        now = datetime.now(UTC)
        was_active = user is not None and user.status == "active"
        manual = set(user.manual_fields or []) if user is not None else set()
        if user is None:
            user = User(display_name=du.name, source="sync")
            session.add(user)
            stats.created += 1
        else:
            stats.updated += 1
            # 管理员手工停用过的人（manual_fields 含 status）不因为「目录里还在」被重新激活
            if user.status == "disabled" and du.active and "status" not in manual:
                user.status = "active"
                stats.reactivated += 1
        if not du.active:
            # 目录内明确标记 status=2（禁用）与 _disable_missing 的"目录中消失"同样是一次
            # active→disabled 的转变，都要计入熔断；已经是 disabled 的维持原状不重复计数。
            if was_active:
                stats.disabled += 1
            user.status = "disabled"
        user.display_name, user.avatar_url = du.name, du.avatar_url  # 每次刷新
        if "position" not in manual:
            user.position = du.position
        for col in ("email", "mobile"):
            value = getattr(du, col)
            if value and getattr(user, col) != value:
                clash_stmt = select(User.id).where(User.id != user.id)
                clash_stmt = (
                    clash_stmt.where(func.lower(User.email) == value.lower())
                    if col == "email"
                    else clash_stmt.where(User.mobile == value)
                )
                if (await session.execute(clash_stmt)).first():
                    stats.field_conflicts.append(f"{du.platform_user_id}:{col}")
                else:
                    setattr(user, col, value)
        await session.flush()  # 拿到 user.id，供下面 login_name 冲突检测、identity/department 使用
        if user.login_name is None:
            wanted = _login_name_for(du)
            taken = (
                await session.execute(
                    select(User.id).where(User.login_name == wanted, User.id != user.id)
                )
            ).first()
            if taken:
                stats.login_name_conflicts.append(du.platform_user_id)
            else:
                user.login_name = wanted
        if ident is None:
            ident = UserIdentity(
                user_id=user.id, platform=platform, platform_user_id=du.platform_user_id
            )
            session.add(ident)
        ident.profile, ident.synced_at = du.profile, now
        if platform == "feishu":
            ident.open_id = du.profile.get("open_id") or None
            ident.union_id = du.profile.get("union_id") or None
        await session.execute(
            delete(UserDepartment).where(
                UserDepartment.user_id == user.id,
                UserDepartment.department_id.in_([d.id for d in dept_map.values()]),
            )
        )
        paths: list[str] = []
        for did in du.dept_ids:
            dept = dept_map.get(did)
            if dept is None:
                continue
            paths.append(dept.path)
            session.add(
                UserDepartment(
                    user_id=user.id, department_id=dept.id, is_primary=(did == du.main_dept_id)
                )
            )
        if user.team_id is None and "team_id" not in manual:
            for rule in rules:
                if (rule.platform in (None, platform)) and any(
                    rule.dept_path_contains in p for p in paths
                ):
                    user.team_id = rule.team_id
                    break
        await session.flush()

    async def _disable_missing(
        self, session: AsyncSession, platform: str, seen: set[str], stats: SyncStats
    ) -> None:
        """把该平台下不在本次目录中的 identity 对应用户停用；source<>'sync' 的行不改。

        seen 为空集时不能写 `not_in([])`——SQLAlchemy 对空集合的 IN/NOT IN 会告警，
        `-W error` 下变成异常；空集直接不加过滤条件，等价于该平台所有人都已消失。
        """
        stmt = (
            select(UserIdentity)
            .options(selectinload(UserIdentity.user))
            .where(UserIdentity.platform == platform)
        )
        if seen:
            stmt = stmt.where(UserIdentity.platform_user_id.not_in(seen))
        rows = (await session.execute(stmt)).scalars().all()
        for ident in rows:
            u = ident.user
            if u.source != "sync" or u.status == "disabled":
                continue
            u.status = "disabled"
            stats.disabled += 1
        await session.flush()
