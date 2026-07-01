"""
Authentication and JWT Token Handler Service.

This module provides classes and functions to manage user authentication:
- Generate access and refresh tokens.
- Decode and validate incoming tokens.
- Define dependency helpers for security routing in FastAPI.
"""

import jwt  # pyjwt library for token generation and decoding
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from datetime import datetime
from enum import Enum
from threading import Lock
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from settings.config import settings # Application configuration settings

# =========================================================
# 1. Singleton Meta-class
# =========================================================
class SingletonMeta(type):
    """
    Metaclass to enforce a Singleton pattern.

    Ensures that only one instance of the class is created across the application.
    Uses a thread lock to ensure thread safety during instantiation.

    Attributes:
        _instances (dict): Cache of active class instances.
        _lock (Lock): Thread lock to prevent race conditions during instantiation.
    """
    _instances = {}       # Stores created instances
    _lock: Lock = Lock()  # Lock object to ensure thread safety

    def __call__(cls, *args, **kwargs):
        """
        Retrieves the existing instance or instantiates a new one if none exists.
        """
        with cls._lock: # Thread synchronization lock
            if cls not in cls._instances:
                # Instantiate base class
                instance = super().__call__(*args, **kwargs)
                cls._instances[cls] = instance
        # Return the cached instance
        return cls._instances[cls]

# =========================================================
# 2. Token Type Enum
# =========================================================
class TokenTypeEnum(Enum):
    """
    Enum representing different token types.

    Attributes:
        ACCESS_TOKEN (int): Short-lived token for general authentication.
        REFRESH_TOKEN (int): Long-lived token used to acquire new access tokens.
    """
    ACCESS_TOKEN = 1   # Short-lived access token
    REFRESH_TOKEN = 2  # Long-lived refresh token

# =========================================================
# 3. Core Authentication Handler
# =========================================================
class AuthHandler(metaclass=SingletonMeta):
    """
    Core class responsible for generating and validating JSON Web Tokens (JWT).

    Maintains session security by encoding user IDs and verifying token signatures.

    Attributes:
        security (HTTPBearer): FastAPI security schema helper.
        secret (str): Secret key used to sign and verify JWT tokens.
    """
    # Swagger UI security helper
    security = HTTPBearer()
    # Read JWT Secret Key from global configuration
    secret = settings.JWT_SECRET_KEY
    
    # --- Internal method: encode token common logic ---
    def _encode_token(self, user_id: int, type: TokenTypeEnum) -> str:
        """
        Creates a signed JWT token containing the user identity and expiration.

        Args:
            user_id (int): The user's database ID.
            type (TokenTypeEnum): Enum value designating token category (Access/Refresh).

        Returns:
            str: Encoded and signed JWT token string.
        """
        # Build token payload
        payload = {
            "iss": str(user_id),              # iss (Issuer): Store user ID (coerced to string)
            "sub": str(int(type.value)),      # sub (Subject): Designated token category (1 or 2)
        }
        
        # Configure expiration thresholds
        if type == TokenTypeEnum.ACCESS_TOKEN:
            exp = datetime.now() + settings.JWT_ACCESS_TOKEN_EXPIRES
        else:
            exp = datetime.now() + settings.JWT_REFRESH_TOKEN_EXPIRES
            
        payload.update({"exp": int(exp.timestamp())})
        
        # Encode token using HS256 algorithm and the shared secret
        return jwt.encode(payload, self.secret, algorithm='HS256')

    # --- External method: Called upon successful login ---
    def encode_login_token(self, user_id: int) -> dict[str, str]:
        """
        Generates a token pair (Access Token & Refresh Token) for user login.

        Args:
            user_id (int): The authenticated user's ID.

        Returns:
            dict[str, str]: Dict containing "access_token", "refresh_token", and "token_type".
        """
        access_token = self._encode_token(user_id, TokenTypeEnum.ACCESS_TOKEN)
        refresh_token = self._encode_token(user_id, TokenTypeEnum.REFRESH_TOKEN)
        
        return {
            "access_token": access_token,   # Short-lived token
            "refresh_token": refresh_token, # Long-lived token
            "token_type": "bearer"          # RFC 6750 standard Token Type
        }
        
    # --- External method: Called when refreshing tokens ---
    def encode_update_token(self, user_id: int) -> dict[str, str]:
        """
        Generates a new access token for active users without requiring re-login.

        Args:
            user_id (int): The user's ID.

        Returns:
            dict[str, str]: Dict containing the new "access_token".
        """
        access_token = self._encode_token(user_id, TokenTypeEnum.ACCESS_TOKEN)
        return {
            "access_token": access_token,
        }

    # --- Verification: Access Token decoder ---
    def decode_access_token(self, token: str) -> int:
        """
        Decodes and verifies the validity of a provided Access Token.

        Args:
            token (str): The incoming Access Token.

        Raises:
            HTTPException: If the token is expired, invalid, or has the wrong type.

        Returns:
            int: The decoded user ID (iss).
        """
        try:
            # 1. Attempt signature validation (will fail if secret doesn't match or token is tampered)
            payload = jwt.decode(token, self.secret, algorithms=['HS256'], options={"verify_sub": False})
            
            # 2. Check token type to avoid token reuse escalation
            sub_value = int(payload['sub']) if isinstance(payload.get('sub'), str) else payload.get('sub')
            if sub_value != int(TokenTypeEnum.ACCESS_TOKEN.value):
                raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail='Token类型错误！')
            
            # 3. Retrieve user ID
            iss_value = payload.get('iss')
            return int(iss_value) if isinstance(iss_value, str) else iss_value
            
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail='Access Token已过期！')
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail='Access Token不可用！')

    # --- Verification: Refresh Token decoder ---
    def decode_refresh_token(self, token: str) -> int:
        """
        Decodes and verifies the validity of a provided Refresh Token.

        Args:
            token (str): The incoming Refresh Token.

        Raises:
            HTTPException: If the token is expired, invalid, or has the wrong type.

        Returns:
            int: The decoded user ID (iss).
        """
        try:
            payload = jwt.decode(token, self.secret, algorithms=['HS256'], options={"verify_sub": False})
            
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
    # 4. FastAPI Dependency Injectors
    # =========================================================
    def auth_access_dependency(self, auth: HTTPAuthorizationCredentials = Security(security)) -> int:
        """
        FastAPI router dependency to validate incoming request access credentials.

        Args:
            auth (HTTPAuthorizationCredentials): Extracted authorization header contents.

        Returns:
            int: User ID derived from validation.
        """
        return self.decode_access_token(auth.credentials)

    def auth_refresh_dependency(self, auth: HTTPAuthorizationCredentials = Security(security)) -> int:
        """
        FastAPI router dependency to validate incoming request refresh credentials.

        Args:
            auth (HTTPAuthorizationCredentials): Extracted authorization header contents.

        Returns:
            int: User ID derived from validation.
        """
        return self.decode_refresh_token(auth.credentials)
