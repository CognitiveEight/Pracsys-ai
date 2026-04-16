"""
Security utilities for authentication and authorization.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.utils.exceptions import SecurityError

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class SecurityManager:
    """Handles security operations like password hashing and JWT tokens."""

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt."""
        return pwd_context.hash(password)

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify a password against its hash."""
        return pwd_context.verify(plain_password, hashed_password)

    @staticmethod
    def create_access_token(
        data: Dict[str, Any], expires_delta: Optional[timedelta] = None
    ) -> str:
        """
        Create a JWT access token.

        Args:
            data: Data to encode in the token
            expires_delta: Optional expiration time delta

        Returns:
            Encoded JWT token
        """
        to_encode = data.copy()

        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(
                minutes=settings.access_token_expire_minutes
            )

        to_encode.update({"exp": expire, "iat": datetime.utcnow()})

        encoded_jwt = jwt.encode(
            to_encode, settings.secret_key, algorithm=settings.algorithm
        )

        return encoded_jwt

    @staticmethod
    def decode_token(token: str) -> Dict[str, Any]:
        """
        Decode and verify a JWT token.

        Args:
            token: JWT token to decode

        Returns:
            Decoded token payload

        Raises:
            SecurityError: If token is invalid or expired
        """
        try:
            payload = jwt.decode(
                token, settings.secret_key, algorithms=[settings.algorithm]
            )
            return payload
        except JWTError as e:
            raise SecurityError("Invalid or expired token", details={"error": str(e)})

    @staticmethod
    def validate_workspace_access(
        user_id: str, workspace_id: str, required_role: Optional[str] = None
    ) -> bool:
        """
        Validate if user has access to workspace.

        Args:
            user_id: User identifier
            workspace_id: Workspace identifier
            required_role: Optional required role for access

        Returns:
            True if access is granted
        """
        # TODO: Implement actual workspace access validation
        # This is a placeholder implementation
        return True

    @staticmethod
    def sanitize_input(input_string: str) -> str:
        """
        Sanitize user input to prevent injection attacks.

        Args:
            input_string: Input string to sanitize

        Returns:
            Sanitized string
        """
        # Basic sanitization - extend as needed
        dangerous_chars = ["<", ">", "&", '"', "'", "/"]
        sanitized = input_string

        for char in dangerous_chars:
            sanitized = sanitized.replace(char, "")

        return sanitized.strip()


# Global security manager instance
security = SecurityManager()
