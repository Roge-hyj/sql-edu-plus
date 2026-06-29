from core.parseval_data_generator import generate_and_compare


def test_mutation_replacement_identifies_where_clause_fix():
    run = generate_and_compare(
        "student(id, name, dept);",
        "SELECT name FROM student WHERE dept = 'CS'",
        "SELECT name FROM student WHERE dept <> 'CS'",
    )

    assert run.executed is True
    where_tests = [item for item in run.mutation_evidence["tests"] if item["clause"] == "WHERE"]
    assert where_tests
    assert where_tests[0]["fixed_by_replacement"] is True
