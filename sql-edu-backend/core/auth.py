import jwt  # 导入 pyjwt 库，用来生成和解密 Token
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from datetime import datetime
from enum import Enum
from threading import Lock
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from settings.config import settings # 导入你的配置文件

# =========================================================
# 1. 单例模式元类 (看不懂没关系，这是 Python 高级写法)
# =========================================================
# 作用：确保 AuthHandler 这个类在整个程序里只会被“创建”一次。
# 就像全公司只有一个财务部，大家都去这一个地方办事，而不是每个人都建一个财务部。
class SingletonMeta(type):
    _instances = {}       # 存放已经创建的实例
    _lock: Lock = Lock()  # 线程锁，防止多个人同时登录时，创建了两个实例

    def __call__(cls, *args, **kwargs):
        with cls._lock: # 加锁，保证安全
            if cls not in cls._instances:
                # 如果还没有创建过实例，就创建一个
                instance = super().__call__(*args, **kwargs)
                cls._instances[cls] = instance
        # 如果已经创建过，直接返回之前那个，不再创建新的
        return cls._instances[cls]

# =========================================================
# 2. Token 类型枚举
# =========================================================
# 作用：给 Token 打上标记，区分它是“短期票”还是“长期票”。
class TokenTypeEnum(Enum):
    ACCESS_TOKEN = 1   # 访问令牌 (短期)
    REFRESH_TOKEN = 2  # 刷新令牌 (长期)

# =========================================================
# 3. 核心认证处理类 (最重要的部分)
# =========================================================
class AuthHandler(metaclass=SingletonMeta):
    # HTTPBearer 是 FastAPI 自带的工具，它会让 Swagger 文档里出现那个“小锁”图标
    security = HTTPBearer()
    # 从配置文件读取加密的密钥 (绝密，不能泄露)
    secret = settings.JWT_SECRET_KEY
    
    # --- 内部方法：生成 Token 的通用逻辑 ---
    def _encode_token(self, user_id: int, type: TokenTypeEnum):
        # payload 是 Token 里面藏的数据（载荷）
        # 注意：pyjwt 新版本要求 iss/sub 必须是字符串，因此这里统一转成 str
        payload = {
            "iss": str(user_id),              # iss (Issuer): 把用户 ID 藏在这里（字符串形式）
            "sub": str(int(type.value)),      # sub (Subject): 标记这个 Token 是类型 1 还是 2（字符串形式）
        }
        
        # 根据类型，设置不同的过期时间
        if type == TokenTypeEnum.ACCESS_TOKEN:
            # 如果是 Access Token，过期时间 = 当前时间 + 配置的短时间(如60分钟)
            exp = datetime.now() + settings.JWT_ACCESS_TOKEN_EXPIRES
        else:
            # 如果是 Refresh Token，过期时间 = 当前时间 + 配置的长时间(如7天)
            exp = datetime.now() + settings.JWT_REFRESH_TOKEN_EXPIRES
            
        # 把过期时间戳也放进 payload 里
        payload.update({"exp": int(exp.timestamp())})
        
        # 【核心步骤】使用密钥(secret)和算法(HS256)进行加密签名，生成字符串
        return jwt.encode(payload, self.secret, algorithm='HS256')

    # --- 外部方法：登录成功后调用 ---
    # 作用：一次性给用户发两张票
    def encode_login_token(self, user_id: int):
        access_token = self._encode_token(user_id, TokenTypeEnum.ACCESS_TOKEN)
        refresh_token = self._encode_token(user_id, TokenTypeEnum.REFRESH_TOKEN)
        
        return {
            "access_token": access_token,   # 短期票
            "refresh_token": refresh_token, # 长期票
            "token_type": "bearer"          # 告诉前端，这是一种 Bearer 类型的 Token
        }
        
    # --- 外部方法：刷新 Token 时调用 ---
    # 作用：当用户拿长期票来换新票时，只发一张新的短期票
    def encode_update_token(self, user_id: int):
        access_token = self._encode_token(user_id, TokenTypeEnum.ACCESS_TOKEN)
        return {
            "access_token": access_token,
        }

    # --- 验证方法：检查 Access Token 是否有效 ---
    def decode_access_token(self, token: str):
        try:
            # 1. 尝试解密 (如果密钥不对，或者被篡改，这里会直接报错)
            # 允许 sub 为数字类型（某些 JWT 库版本要求 sub 为字符串，但我们使用数字）
            payload = jwt.decode(token, self.secret, algorithms=['HS256'], options={"verify_sub": False})
            
            # 2. 检查 Token 类型 (防止用户拿 Refresh Token 当 Access Token 用)
            # 将 sub 转换为整数进行比较（兼容字符串和数字类型）
            sub_value = int(payload['sub']) if isinstance(payload.get('sub'), str) else payload.get('sub')
            if sub_value != int(TokenTypeEnum.ACCESS_TOKEN.value):
                raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail='Token类型错误！')
            
            # 3. 验证通过，返回藏在里面的 User ID（转成 int，兼容字符串形式）
            iss_value = payload.get('iss')
            return int(iss_value) if isinstance(iss_value, str) else iss_value
            
        except jwt.ExpiredSignatureError:
            # 如果时间到了
            raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail='Access Token已过期！')
        except jwt.InvalidTokenError:
            # 如果是乱码或伪造的
            raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail='Access Token不可用！')

    # --- 验证方法：检查 Refresh Token 是否有效 ---
    def decode_refresh_token(self, token: str):
        # Refresh Token 报错通常返回 401 (未授权)，提示前端跳转回登录页
        try:
            # 允许 sub 为数字类型
            payload = jwt.decode(token, self.secret, algorithms=['HS256'], options={"verify_sub": False})
            # 将 sub 转换为整数进行比较（兼容字符串和数字类型）
            sub_value = int(payload.get('sub')) if isinstance(payload.get('sub'), str) else payload.get('sub')
            if sub_value != int(TokenTypeEnum.REFRESH_TOKEN.value):
                raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail='Token类型错误！')
            iss_value = payload.get('iss')
            return int(iss_value) if isinstance(iss_value, str) else iss_value
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail='Refresh Token已过期！')
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail='Refresh Token不可用！')

    # =========================================================
    # 4. FastAPI 依赖注入 (Bouncer / 门卫)
    # =========================================================
    # 以后写接口时，只要在参数里写: user_id = Depends(AuthHandler().auth_access_dependency)
    # FastAPI 就会自动执行下面的代码：
    
    def auth_access_dependency(self, auth: HTTPAuthorizationCredentials = Security(security)):
        # 1. 自动从请求头 Authorization: Bearer xxxx 中提取 token
        # 2. 调用 decode_access_token 进行验证
        # 3. 如果验证失败，直接抛出异常，接口代码不会执行
        # 4. 如果验证成功，返回 user_id 给接口函数
        return self.decode_access_token(auth.credentials)

    def auth_refresh_dependency(self, auth: HTTPAuthorizationCredentials = Security(security)):
        # 同上，专门用于验证 Refresh Token 的接口
        return self.decode_refresh_token(auth.credentials)