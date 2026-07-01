from unittest.mock import patch, AsyncMock
import pytest
from sqlalchemy import text
from routers.ai import check_sql, SQLCheckRequest
from schemas.agent import SQLCheckResultSchema


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
    """测试正常 SQL 执行和归因闭环流程。"""
    # 创建测试用表并插入示例数据
    await test_db_session.execute(text("CREATE TABLE IF NOT EXISTS students (id INT, age INT)"))
    await test_db_session.execute(text("INSERT INTO students (id, age) VALUES (1, 20)"))
    await test_db_session.commit()

    # 更新题目的 correct_sql，使其能在 sqlite 正常运行，并设 schema_preview 为 None 避免 DROP 并重建表（SQLite 不支持 MySQL schema_preview DDL）
    test_question.correct_sql = "SELECT * FROM students WHERE age > 18"
    test_question.schema_preview = None
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
    assert response.error_message in (None, "结果匹配。", "结果匹配（含顺序）。")
    assert "恭喜" in response.hint["overall_comment"]


@pytest.mark.asyncio
@patch("routers.ai.execute_setup_sql", new_callable=AsyncMock)
async def test_check_sql_observation_stage_1(mock_execute_setup_sql, test_db_session, test_user, test_question):
    """测试 Stage 1 Observe 数据采集，检查 E_AST/E_data/E_MUT 证据包。"""
    # 创建测试用表并插入示例数据
    await test_db_session.execute(text("CREATE TABLE IF NOT EXISTS students (id INT, age INT, name VARCHAR(255))"))
    await test_db_session.execute(text("INSERT INTO students (id, age, name) VALUES (1, 20, 'Alice')"))
    await test_db_session.execute(text("INSERT INTO students (id, age, name) VALUES (2, 22, 'Bob')"))
    await test_db_session.commit()

    # 设置题目元数据和 schema_preview
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
    assert e_data["student_rows"] == 2
    assert e_data["correct_rows"] == 1

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
