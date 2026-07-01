"""测试 API 集成（需要运行中的服务）。"""

import pytest
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.mark.asyncio
class TestAIApi:
    """测试 AI 接口。"""

    async def test_sql_hint_endpoint(self):
        """测试简化版 SQL 提示接口。"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/ai/sql-hint",
                json={"sql": "SELECT * FROM users WHERE age > 18"}
            )
            
            assert response.status_code == 200
            data = response.json()
            assert "hint" in data
            assert "diagnoses" in data["hint"] or "overall_comment" in data["hint"]

    async def test_check_sql_without_auth(self):
        """测试未认证的请求应该被拒绝。"""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/ai/check-sql",
                json={
                    "student_sql": "SELECT * FROM users",
                    "question_id": 1
                }
            )
            
            # 应该返回 403 或 401
            assert response.status_code in [401, 403]

    async def test_check_sql_invalid_question(self):
        """测试不存在的题目 ID。"""
        # 注意：这个测试需要有效的 Token
        # 在实际测试中，应该先登录获取 Token
        pass  # 需要 mock 认证或使用测试 Token
