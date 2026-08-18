import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from models.submission import Submission
from routers.ai import check_sql, SQLCheckRequest
from schemas.agent import SQLCheckResultSchema


@pytest.fixture(autouse=True)
def _use_sqlite_compatibility_backend(monkeypatch):
    monkeypatch.setattr("routers.ai.settings.PARSEVAL_EXECUTION_BACKEND", "sqlite")


@pytest.mark.asyncio
async def test_check_sql_syntax_error(
    test_db_session,
    test_user,
    test_question,
):
    """测试 SQL 语法错误时触发早期检查并直接返回语法纠错提示。"""
    payload = SQLCheckRequest(
        student_sql="SELECT * FROM (students",  # 语法错误，缺失括号
        question_id=test_question.id,
    )

    response = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.is_safety_blocked is False
    assert "SQL 语法错误" in response.error_message
    assert "语法" in response.hint["overall_comment"] or "syntax" in response.hint["overall_comment"]


@pytest.mark.asyncio
async def test_check_sql_safety_blocked(test_db_session, test_user, test_question):
    """测试安全检查拦截危险 SQL 操作。"""
    payload = SQLCheckRequest(
        student_sql="DROP TABLE students",  # 危险 SQL
        question_id=test_question.id,
    )

    response = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.is_safety_blocked is True
    assert "危险操作" in response.error_message or "安全拦截" in response.hint["overall_comment"]


@pytest.mark.asyncio
async def test_check_sql_normal_flow(test_db_session, test_user, test_question):
    """测试正常 SQL 执行和归因闭环流程（ParSEval 为唯一判题来源）。"""
    # 设置 schema_preview 供 ParSEval 造数判题
    test_question.correct_sql = "SELECT * FROM students WHERE age > 18"
    test_question.schema_preview = '{"tables":[{"name":"students","columns":["id","age"],"rows":[{"id":1,"age":20}]}]}'
    test_db_session.add(test_question)
    await test_db_session.flush()

    payload = SQLCheckRequest(
        student_sql="SELECT * FROM students WHERE age > 18",
        question_id=test_question.id,
    )

    response = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is True
    assert response.is_safety_blocked is False
    assert "恭喜" in response.hint["overall_comment"]


@pytest.mark.asyncio
async def test_check_sql_observation_stage_1(test_db_session, test_user, test_question):
    """测试 Stage 1 Observe 数据采集，检查 E_AST/E_data/E_MUT 证据包。"""
    # 设置题目元数据和 schema_preview（ParSEval 造数用）
    test_question.correct_sql = "SELECT name FROM students WHERE age > 20"
    test_question.schema_preview = '{"tables":[{"name":"students","columns":["id","age","name"],"rows":[{"id":1,"age":20,"name":"Alice"},{"id":2,"age":22,"name":"Bob"}]}]}'
    test_db_session.add(test_question)
    await test_db_session.flush()

    # 学生 SQL 故意少些了 WHERE 条件
    payload = SQLCheckRequest(
        student_sql="SELECT name FROM students",
        question_id=test_question.id,
    )

    response = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.observation is not None
    
    # 校验 E_AST 结构传感器
    e_ast = response.observation["E_AST"]
    assert e_ast["student_parse_ok"] is True
    assert e_ast["standard_parse_ok"] is True
    assert e_ast["student_features"]["has_where"] is False
    assert e_ast["standard_features"]["has_where"] is True

    # 校验 E_data 数据传感器
    e_data = response.observation["E_data"]
    assert e_data["is_correct"] is False
    assert "行数" in e_data["error_message"] or "数据" in e_data["error_message"]
    # ParSEval 动态造数：学生无 WHERE 返回更多行，标准有 WHERE 返回更少行
    assert e_data["student_rows"] > e_data["correct_rows"]

    # 校验 E_MUT 变分隔离传感器
    e_mut = response.observation["E_MUT"]
    assert e_mut["enabled"] is True
    # 检查是否有 WHERE 的变分隔离测试
    mutation_tests = e_mut["mutation_tests"]
    assert len(mutation_tests) > 0
    where_mutations = [t for t in mutation_tests if t["clause"] == "WHERE"]
    assert len(where_mutations) > 0
    # 由于学生缺少 WHERE，替换为标答 WHERE 后应该能通过
    assert where_mutations[0]["fixed_by_replacement"] is True


@pytest.mark.asyncio
async def test_check_sql_unsupported_dialect_feature_is_not_attributed_to_student(
    test_db_session,
    test_user,
    test_question,
):
    test_question.correct_sql = (
        "SELECT region, product, SUM(amount) FROM sales "
        "GROUP BY ROLLUP(region, product)"
    )
    test_question.schema_preview = (
        '{"tables":[{"name":"sales","columns":["region","product","amount"],'
        '"rows":[{"region":"East","product":"A","amount":10}]}]}'
    )
    test_db_session.add(test_question)
    await test_db_session.flush()

    payload = SQLCheckRequest(
        student_sql="SELECT region, product, SUM(amount) FROM sales GROUP BY region, product",
        question_id=test_question.id,
    )

    with pytest.raises(HTTPException) as caught:
        await check_sql(
            payload=payload,
            user_id=test_user.id,
            session=test_db_session,
        )

    assert getattr(caught.value, "status_code", None) == 422
    assert caught.value.detail["code"] == "UNSUPPORTED"
    assert await test_db_session.scalar(
        select(func.count()).select_from(Submission)
    ) == 0
