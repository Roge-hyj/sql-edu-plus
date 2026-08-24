"""Persistence-backed rate limits and one-time email captcha behavior."""

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from models.auth import EmailCaptcha
from repository.user_repo import EmailCodeRepository
from routers.auth import get_email_captcha
from settings.config import settings


@pytest.fixture
def captcha_policy(monkeypatch: pytest.MonkeyPatch):
    values = {
        "CAPTCHA_EXPIRE_MINUTES": 10,
        "CAPTCHA_SEND_INTERVAL_SECONDS": 60,
        "CAPTCHA_DAILY_EMAIL_LIMIT": 5,
        "CAPTCHA_DAILY_IP_LIMIT": 20,
        "CAPTCHA_MAX_VERIFY_ATTEMPTS": 3,
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


@pytest.mark.asyncio
async def test_send_limits_cover_interval_email_daily_and_ip(
    test_db_session, captcha_policy
):
    repo = EmailCodeRepository(test_db_session)
    now = datetime(2026, 8, 24, 9, 0, 0)

    allowed, retry_after, reason = await repo.send_limit_status(
        "Student@Example.com", "203.0.113.10", now=now
    )
    assert (allowed, retry_after, reason) == (True, 0, None)
    await repo.add_email_captcha("Student@Example.com", "123456", "203.0.113.10", now=now)
    await test_db_session.commit()

    allowed, retry_after, reason = await repo.send_limit_status(
        "student@example.com", "203.0.113.10", now=now + timedelta(seconds=30)
    )
    assert not allowed and retry_after == 30 and reason == "email_interval"

    # Five historical sends consume the per-email daily budget.
    for index in range(1, 5):
        send_time = now + timedelta(minutes=index + 2)
        await repo.add_email_captcha(
            "student@example.com", f"{index:06d}", "203.0.113.10", now=send_time
        )
    await test_db_session.commit()
    allowed, _, reason = await repo.send_limit_status(
        "student@example.com", "203.0.113.10", now=now + timedelta(hours=1)
    )
    assert not allowed and reason == "email_daily_limit"

    other_email = "other@example.com"
    for index in range(settings.CAPTCHA_DAILY_IP_LIMIT):
        await repo.add_email_captcha(
            other_email + str(index), f"{index:06d}", "198.51.100.5", now=now
        )
    await test_db_session.commit()
    allowed, _, reason = await repo.send_limit_status(
        "new@example.com", "198.51.100.5", now=now + timedelta(hours=1)
    )
    assert not allowed and reason == "ip_daily_limit"


@pytest.mark.asyncio
async def test_wrong_captcha_attempts_are_counted_and_eventually_locked(
    test_db_session, captcha_policy
):
    repo = EmailCodeRepository(test_db_session)
    created = datetime(2026, 8, 24, 9, 0, 0)
    record = await repo.add_email_captcha(
        "student@example.com", "123456", "203.0.113.10", now=created
    )
    await test_db_session.commit()

    assert await repo.verify_email_captcha("student@example.com", "000000", now=created) == "invalid"
    await test_db_session.commit()
    assert await repo.verify_email_captcha("student@example.com", "000000", now=created) == "invalid"
    await test_db_session.commit()
    assert (
        await repo.verify_email_captcha("student@example.com", "000000", now=created)
        == "attempts_exceeded"
    )
    await test_db_session.commit()

    refreshed = await test_db_session.get(EmailCaptcha, record.id)
    assert refreshed is not None
    assert refreshed.failed_attempts == 3
    assert refreshed.used is True
    assert await repo.verify_email_captcha("student@example.com", "123456", now=created) == "invalid"


@pytest.mark.asyncio
async def test_captcha_expires_and_success_is_one_time(
    test_db_session, captcha_policy
):
    repo = EmailCodeRepository(test_db_session)
    created = datetime(2026, 8, 24, 9, 0, 0)
    await repo.add_email_captcha("expired@example.com", "123456", "203.0.113.10", now=created)
    await test_db_session.commit()
    assert (
        await repo.verify_email_captcha(
            "expired@example.com", "123456", now=created + timedelta(minutes=10, seconds=1)
        )
        == "expired"
    )

    await repo.add_email_captcha("one-time@example.com", "654321", "203.0.113.10", now=created)
    await test_db_session.commit()
    assert await repo.verify_email_captcha("one-time@example.com", "654321", now=created) == "ok"
    await test_db_session.commit()
    assert await repo.verify_email_captcha("one-time@example.com", "654321", now=created) == "invalid"


@pytest.mark.asyncio
async def test_code_endpoint_returns_429_and_uses_six_digit_secret_code(
    test_db_session, captcha_policy, monkeypatch: pytest.MonkeyPatch
):
    class FakeMail:
        def __init__(self):
            self.messages = []

        async def send_message(self, message):
            self.messages.append(message)

    class FakeRequest:
        client = type("Client", (), {"host": "203.0.113.44"})()

    fake_mail = FakeMail()
    monkeypatch.setattr("routers.auth.secrets.randbelow", lambda _: 42)
    result = await get_email_captcha(
        "student@example.com", FakeRequest(), fake_mail, test_db_session
    )
    assert result.result == "success"
    assert "000042" in fake_mail.messages[0].body

    with pytest.raises(HTTPException) as raised:
        await get_email_captcha(
            "student@example.com", FakeRequest(), fake_mail, test_db_session
        )
    assert raised.value.status_code == 429
    assert raised.value.headers == {"Retry-After": "60"}
