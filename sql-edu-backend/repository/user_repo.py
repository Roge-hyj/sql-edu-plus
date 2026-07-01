from sqlalchemy.ext.asyncio import AsyncSession
from models.auth import EmailCaptcha
from sqlalchemy import select,delete,exists,update,desc
from datetime import datetime,timedelta

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
    
    async def add_email_captcha(self, email: str, captcha: str)->EmailCaptcha:
            await self.session.execute(delete(EmailCaptcha).where(EmailCaptcha.email == email))
            email_captcha = EmailCaptcha(email=email, captcha=captcha)
            self.session.add(email_captcha)
            await self.session.flush() # 预写入，但不锁死事务
            return email_captcha

    async def check_email_captcha(self, email: str, captcha: str) -> bool:
            stmt =select(EmailCaptcha).where(EmailCaptcha.email == email, 
                                             EmailCaptcha.captcha == captcha.strip(), 
                                             EmailCaptcha.used == False
                                             ).order_by(desc(EmailCaptcha.created_at)).limit(1)

            email_captcha:EmailCaptcha|None= await self.session.scalar(stmt)
            if email_captcha is None:
                return False
            if(datetime.utcnow() - email_captcha.created_at)>timedelta(minutes=10):
                return False
            return True
    
    
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

        