from sqlalchemy.ext.asyncio import AsyncSession
from models.auth import EmailCaptcha
from sqlalchemy import select,delete,exists,update,desc,func
from datetime import datetime,timedelta,time
import hmac

from settings.config import settings

from models.user import User
from schemas.user import UserCreateSchema
class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
    async def get_by_email(self, email: str)->User|None:
            user=await self.session.scalar(select(User).where(User.email==email))
            return user
    
    async def get_by_username(self, username: str)->User|None:
            user=await self.session.scalar(select(User).where(User.username==username))
            return user
    
    async def get_by_email_or_username(self, identifier: str)->User|None:
            """根据邮箱或用户名查找用户。
            
            :param identifier: 邮箱或用户名
            :return: User 对象，如果不存在则返回 None
            """
            # 先尝试按邮箱查找
            user = await self.get_by_email(identifier)
            if user:
                return user
            # 如果邮箱查找失败，尝试按用户名查找
            return await self.get_by_username(identifier)
    async def email_is_exist(self,email:str)->bool:
            stmt=select(exists().where(User.email==email))
            return await self.session.scalar(stmt)
    async def create_user(self, user_schema: UserCreateSchema) -> User:
        user = User(
            email=user_schema.email,
            username=user_schema.username,
            password=user_schema.password,
            role=user_schema.role,
        )
        self.session.add(user)
        return user
    
    async def get_by_id(self, user_id: int) -> User | None:
        """根据 ID 查询用户。

        :param user_id: 用户 ID
        :return: User 对象，如果不存在则返回 None
        """
        stmt = select(User).where(User.id == user_id)
        user = await self.session.scalar(stmt)
        return user
    
    async def delete_user(self, user_id: int) -> bool:
        """删除用户及其所有相关数据（级联删除）。

        :param user_id: 用户 ID
        :return: 如果删除成功返回 True，否则返回 False
        """
        from models.submission import Submission

        user = await self.get_by_id(user_id)
        if not user:
            return False
        # 手动删除关联的提交记录（确保级联删除生效）
        # 即使数据库有 CASCADE 约束，手动删除更可靠
        await self.session.execute(
            delete(Submission).where(Submission.user_id == user_id)
        )
        
        # 删除用户
        await self.session.delete(user)
        return True
    # 其他用户相关的数据库操作方法可以放在这里

#email captcha 相关的数据库操作的仓库类
class EmailCodeRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
    
    @staticmethod
    def _email_key(email: str) -> str:
        return str(email).strip().lower()

    @staticmethod
    def _day_start(now: datetime) -> datetime:
        return datetime.combine(now.date(), time.min)

    async def send_limit_status(
        self, email: str, ip_address: str, *, now: datetime | None = None
    ) -> tuple[bool, int, str | None]:
        """Return ``(allowed, retry_after, reason)`` for a new captcha send."""
        now = now or datetime.utcnow()
        email_key = self._email_key(email)
        latest = await self.session.scalar(
            select(EmailCaptcha)
            .where(EmailCaptcha.email == email_key)
            .order_by(EmailCaptcha.created_at.desc(), EmailCaptcha.id.desc())
            .limit(1)
        )
        interval = timedelta(seconds=settings.CAPTCHA_SEND_INTERVAL_SECONDS)
        if latest is not None and now - latest.created_at < interval:
            retry_after = max(1, int((interval - (now - latest.created_at)).total_seconds() + 0.999))
            return False, retry_after, "email_interval"

        day_start = self._day_start(now)
        email_count = await self.session.scalar(
            select(func.count(EmailCaptcha.id)).where(
                EmailCaptcha.email == email_key,
                EmailCaptcha.created_at >= day_start,
            )
        )
        if int(email_count or 0) >= settings.CAPTCHA_DAILY_EMAIL_LIMIT:
            return False, 86400, "email_daily_limit"

        ip_count = await self.session.scalar(
            select(func.count(EmailCaptcha.id)).where(
                EmailCaptcha.ip_address == ip_address,
                EmailCaptcha.created_at >= day_start,
            )
        )
        if int(ip_count or 0) >= settings.CAPTCHA_DAILY_IP_LIMIT:
            return False, 86400, "ip_daily_limit"
        return True, 0, None

    async def add_email_captcha(
        self,
        email: str,
        captcha: str,
        ip_address: str,
        *,
        now: datetime | None = None,
    ) -> EmailCaptcha:
        """Persist a new captcha without deleting history used for rate limits."""
        email_captcha = EmailCaptcha(
            email=self._email_key(email),
            captcha=captcha,
            ip_address=ip_address,
            created_at=now or datetime.utcnow(),
        )
        self.session.add(email_captcha)
        await self.session.flush()
        return email_captcha

    async def verify_email_captcha(
        self, email: str, captcha: str, *, now: datetime | None = None
    ) -> str:
        """Validate and consume the latest captcha.

        Returns ``ok``, ``invalid``, ``expired`` or ``attempts_exceeded``. The
        successful path marks the row used before returning, so callers cannot
        accidentally validate the same code twice.
        """
        now = now or datetime.utcnow()
        email_captcha: EmailCaptcha | None = await self.session.scalar(
            select(EmailCaptcha)
            .where(EmailCaptcha.email == self._email_key(email))
            .order_by(EmailCaptcha.created_at.desc(), EmailCaptcha.id.desc())
            .limit(1)
            .with_for_update()
        )
        if email_captcha is None or email_captcha.used:
            return "invalid"
        if now - email_captcha.created_at > timedelta(minutes=settings.CAPTCHA_EXPIRE_MINUTES):
            return "expired"
        if email_captcha.failed_attempts >= settings.CAPTCHA_MAX_VERIFY_ATTEMPTS:
            return "attempts_exceeded"
        if not hmac.compare_digest(email_captcha.captcha, captcha.strip()):
            email_captcha.failed_attempts += 1
            if email_captcha.failed_attempts >= settings.CAPTCHA_MAX_VERIFY_ATTEMPTS:
                email_captcha.used = True
                email_captcha.used_at = now
                await self.session.flush()
                return "attempts_exceeded"
            await self.session.flush()
            return "invalid"

        email_captcha.used = True
        email_captcha.used_at = now
        await self.session.flush()
        return "ok"

    async def check_email_captcha(self, email: str, captcha: str) -> bool:
        """Compatibility wrapper for callers that only need a boolean result."""
        return (await self.verify_email_captcha(email, captcha)) == "ok"
    
    
    async def mark_captcha_used(self, email: str, captcha: str):
        stmt = (update(EmailCaptcha).where(EmailCaptcha.email == email, EmailCaptcha.captcha == captcha).values(used=True)
    )
        await self.session.execute(stmt)   

    async def delete_captcha_record(self, email: str, captcha: str):
        """【补偿逻辑】如果发送邮件失败，删除刚刚生成的记录"""
        stmt = delete(EmailCaptcha).where(
                EmailCaptcha.email == email,
                EmailCaptcha.captcha == captcha
            )
        await self.session.execute(stmt)   

    async def delete_captcha_by_id(self, captcha_id: int) -> None:
        """Delete exactly one unsent captcha during SMTP compensation."""
        await self.session.execute(delete(EmailCaptcha).where(EmailCaptcha.id == captcha_id))
