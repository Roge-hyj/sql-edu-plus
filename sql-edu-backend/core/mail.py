"""
Email service configuration and instance creation helper.

This module provides the necessary configuration for creating email clients
using fastapi_mail, loading settings from the project configuration.
"""

from fastapi_mail import FastMail, ConnectionConfig
from settings.config import settings

def create_mail_instance() -> FastMail:
    """
    Creates and returns a FastMail instance configured with project settings.

    Returns:
        FastMail: Configured FastAPI-Mail client instance.
    """
    # Establish connection configuration utilizing global project settings
    mail_config = ConnectionConfig(
        MAIL_USERNAME=settings.MAIL_USERNAME,
        MAIL_PASSWORD=settings.MAIL_PASSWORD, # Ensure this is a string type in config.py
        MAIL_FROM=settings.MAIL_FROM,
        MAIL_PORT=settings.MAIL_PORT,
        MAIL_SERVER=settings.MAIL_SERVER,
        MAIL_FROM_NAME=settings.MAIL_FROM_NAME,
        
        # Critical settings mapping STARTTLS and SSL/TLS statuses dynamically
        MAIL_STARTTLS=settings.MAIL_STARTTLS, # False
        MAIL_SSL_TLS=settings.MAIL_SSL_TLS,   # True
        
        USE_CREDENTIALS=True,
        # Disable certificate validation in development environment to avoid SSL connection errors
        VALIDATE_CERTS=False 
    )
    return FastMail(mail_config)
