import json
import re
from pathlib import Path

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "source_pdfs"
OUT = ROOT / "outputs" / "data_std_full.json"

UNIVERSITY_SCHEMA = (
    "department(dept_name, building, budget); "
    "course(course_id, title, dept_name, credits); "
    "instructor(ID, name, dept_name, salary); "
    "section(course_id, sec_id, semester, year, building, room_number, time_slot_id); "
    "teaches(ID, course_id, sec_id, semester, year); "
    "student(ID, name, dept_name, tot_cred); "
    "takes(ID, course_id, sec_id, semester, year, grade); "
    "advisor(s_ID, i_ID); "
    "prereq(course_id, prereq_id); "
    "classroom(building, room_number, capacity); "
    "time_slot(time_slot_id, day, start_hr, start_min, end_hr, end_min)"
)

COMPANY_SCHEMA = (
    "EMPLOYEE(Fname, Minit, Lname, Ssn, Bdate, Address, Sex, Salary, Super_ssn, Dno); "
    "DEPARTMENT(Dname, Dnumber, Mgr_ssn, Mgr_start_date); "
    "DEPT_LOCATIONS(Dnumber, Dlocation); "
    "PROJECT(Pname, Pnumber, Plocation, Dnum); "
    "WORKS_ON(Essn, Pno, Hours); "
    "DEPENDENT(Essn, Dependent_name, Sex, Bdate, Relationship)"
)

NORTHWIND_SCHEMA = (
    "Products(ProductID, ProductName, SupplierID, CategoryID, QuantityPerUnit, UnitPrice, UnitsInStock, UnitsOnOrder, ReorderLevel, Discontinued); "
    "Categories(CategoryID, CategoryName, Description); "
    "Customers(CustomerID, CompanyName, ContactName, ContactTitle, Address, City, Region, PostalCode, Country, Phone); "
    "Suppliers(SupplierID, CompanyName, ContactName, ContactTitle, Address, City, Region, PostalCode, Country, Phone); "
    "Orders(OrderID, CustomerID, EmployeeID, OrderDate, RequiredDate, ShippedDate, ShipVia, Freight, ShipName, ShipAddress, ShipCity, ShipRegion, ShipPostalCode, ShipCountry); "
    "[Order Details](OrderID, ProductID, UnitPrice, Quantity, Discount); "
    "Employees(EmployeeID, LastName, FirstName, Title, ReportsTo, HireDate, City, Country); "
    "Territories(TerritoryID, TerritoryDescription, RegionID); "
    "EmployeeTerritories(EmployeeID, TerritoryID); "
    "Shippers(ShipperID, CompanyName)"
)


def normalize_text(text):
    replacements = {
        "\t": " ",
        "\u00a0": " ",
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def clean_spaces(text):
    return " ".join(text.strip().split())


def infer_tags(sql):
    s = sql.upper()
    l2 = []
    if re.search(r"\bSELECT\s+\*", s) or re.search(r"\bSELECT\b", s):
        l2.append("PROJ_COL")
    if re.search(r"\b[A-Z_][A-Z0-9_.]*\s*(\+|-|\*|/|%)\s*[A-Z_0-9'.]", s) or any(fn in s for fn in ["CASE ", "ISNULL", "COALESCE"]):
        l2.append("PROJ_EXPR")
    if re.search(r"\bAS\b", s):
        l2.append("ALIAS_COL")
    if re.search(r"\bDISTINCT\b", s):
        l2.append("DISTINCT_SET")
    if re.search(r"\bTOP\b|\bLIMIT\b|\bFETCH\b", s):
        l2.append("LIMIT_OFF")
    if re.search(r"\bWHERE\b", s):
        l2.append("COMP_VAL")
    if re.search(r"\bIS\s+NULL\b|\bIS\s+NOT\s+NULL\b", s):
        l2.append("COMP_NULL")
    if re.search(r"\bAND\b|\bOR\b", s):
        l2.append("LOGIC_AND_OR")
    if re.search(r"\bNOT\b|<>", s):
        l2.append("LOGIC_NOT")
    if re.search(r"\bBETWEEN\b|>=|<=|<|>", s):
        l2.append("RANGE_BET")
    if re.search(r"\bIN\s*\(", s):
        l2.append("SET_IN")
    if re.search(r"\bLIKE\b", s):
        l2.append("LIKE_STR")
    if re.search(r"\bORDER\s+BY\b", s):
        l2.append("SORT_ASC")
    if re.search(r"\bDESC\b", s):
        l2.append("SORT_DESC")
    if re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", s):
        l2.append("AGG_BASIC")
    if re.search(r"\bGROUP\s+BY\b", s):
        l2.append("GB_SIMPLE")
    if re.search(r"\bHAVING\b", s):
        l2.append("HV_SIMPLE")
    if re.search(r"\bJOIN\b", s):
        l2.append("JOIN_INNER")
    if re.search(r"\bLEFT\b", s):
        l2.append("JOIN_LEFT")
    if re.search(r"\bRIGHT\b", s):
        l2.append("JOIN_RIGHT")
    if re.search(r"\bFULL\b", s):
        l2.append("JOIN_FULL")
    if re.search(r"\bCROSS\b|ON\s+1\s*=\s*1", s):
        l2.append("JOIN_CROSS")
    if re.search(r"\bON\b", s):
        l2.append("JOIN_ON")
    if re.search(r"\bNATURAL\b", s):
        l2.append("JOIN_NATURAL")
    if re.search(r"\(\s*SELECT\b", s):
        l2.append("SUB_TABLE" if re.search(r"\bFROM\s*\(\s*SELECT\b", s) else "SUB_IN_ALL_ANY")
    if re.search(r"\bEXISTS\b", s):
        l2.append("SUB_EXISTS")
    if re.search(r"\bALL\b|\bANY\b|\bSOME\b", s):
        l2.append("SUB_IN_ALL_ANY")
    if re.search(r"\bUNION\b", s):
        l2.append("SET_UNION")
    if re.search(r"\bINTERSECT\b", s):
        l2.append("SET_INTERSECT")
    if re.search(r"\bEXCEPT\b|\bMINUS\b", s):
        l2.append("SET_EXCEPT")
    if re.search(r"\bWITH\b", s):
        l2.append("CTE_SIMPLE")
    if re.search(r"\bRECURSIVE\b", s):
        l2.append("CTE_RECURSIVE")
    if re.search(r"\bOVER\b", s):
        l2.append("WIN_OVER")
    if re.search(r"\bRANK\b|\bROW_NUMBER\b", s):
        l2.append("WIN_RANK")
    if re.search(r"\bROLLUP\b|\bCUBE\b|GROUPING\s*\(", s):
        l2.append("WIN_FRAME")
    if re.search(r"\bISNULL\b|\bCOALESCE\b|NULLIF\b", s):
        l2.append("NULL_COAL")
    if not l2:
        l2 = ["PROJ_COL"]

    advanced = {"WIN_OVER", "WIN_RANK", "WIN_LEAD_LAG", "WIN_FRAME", "CTE_SIMPLE", "CTE_RECURSIVE", "SET_UNION", "SET_INTERSECT", "SET_EXCEPT", "NULL_COAL"}
    if any(k in advanced for k in l2):
        l1 = "KP_ADVANCED"
    elif any(k.startswith("SUB_") for k in l2):
        l1 = "KP_SUBQUERY"
    elif any(k.startswith("JOIN_") for k in l2):
        l1 = "KP_JOIN"
    elif any(k.startswith("AGG_") or k.startswith("GB_") or k.startswith("HV_") for k in l2):
        l1 = "KP_AGG"
    elif any(k.startswith("SORT_") for k in l2):
        l1 = "KP_ORDER"
    elif any(k in {"STR_CASE", "STR_SUB", "NUM_ROUND", "DATE_EXT", "DATE_DIFF", "CASE_SIMPLE", "CASE_SEARCH", "TYPE_CAST"} for k in l2):
        l1 = "KP_FUNC"
    elif any(k in {"COMP_VAL", "COMP_NULL", "LOGIC_AND_OR", "LOGIC_NOT", "RANGE_BET", "SET_IN", "LIKE_STR"} for k in l2):
        l1 = "KP_FILTER"
    else:
        l1 = "KP_BASIC"
    return l1, list(dict.fromkeys(l2))


def infer_difficulty(l1, l2):
    base = {
        "KP_BASIC": 1.5,
        "KP_FILTER": 3.0,
        "KP_ORDER": 3.0,
        "KP_AGG": 5.0,
        "KP_JOIN": 5.5,
        "KP_SUBQUERY": 7.0,
        "KP_FUNC": 4.0,
        "KP_ADVANCED": 8.0,
    }[l1]
    return round(min(10.0, base + max(0, len(l2) - 2) * 0.3), 1)


def add(items, q, ans_sql, schema, source, difficulty=None, l1=None, l2=None):
    if ans_sql.strip().upper().startswith("CREATE VIEW"):
        return
    inferred_l1, inferred_l2 = infer_tags(ans_sql)
    l1 = l1 or inferred_l1
    l2 = l2 or inferred_l2
    items.append({
        "id": len(items) + 1,
        "difficulty": difficulty if difficulty is not None else infer_difficulty(l1, l2),
        "l1": l1,
        "l2": l2,
        "schema": schema,
        "q": clean_spaces(q),
        "ans_sql": clean_spaces(ans_sql).rstrip(";") + ";",
        "source": source,
    })


def extract_learn_sql_fast(items):
    pdf = next(PDF_DIR.glob("SQL_ with practice exercises*.pdf"))
    text = "\n".join((page.extract_text() or "") for page in PdfReader(str(pdf)).pages)
    text = normalize_text(text)
    ex_matches = list(re.finditer(r"Exercise\s*-\s*(\d+\.\d+)\s*\(answer\)", text, re.I))
    ans_matches = list(re.finditer(r"Answer\s*-\s*(\d+\.\d+)\s*\(back\)", text, re.I))

    cut_markers = [
        "\nLinked answers", "\nA note on", "\nConstants", "\nSELECT without FROM",
        "\nModulus", "\nBrackets", "\nString literals", "\nThe IsNull Function",
        "\nComments", "\nClauses", "\nMatching Values", "\nMatching text",
        "\nDate values", "\nAND -", "\nOR -", "\nDISTINCT -", "\nORDER BY -",
        "\nTOP -", "\nOther Aggregate", "\nGrouping", "\nHAVING -",
        "\nAlias -", "\nOther ways to join", "\nOuter Joins", "\nFull Outer Joins",
        "\nCross Join", "\nUNION", "\nEXCEPT", "\nLearning from", "\nSubqueries",
        " Text Strings ", " Functions ", " Other functions ", " Applying this to other tables ",
    ]
    questions = {}
    for idx, match in enumerate(ex_matches):
        end = ex_matches[idx + 1].start() if idx + 1 < len(ex_matches) else text.find("Answer - 1.1")
        body = text[match.end():end].strip()
        for marker in cut_markers:
            pos = body.find(marker)
            if pos > 0:
                body = body[:pos]
        questions[match.group(1)] = clean_spaces(body)

    answers = {}
    for idx, match in enumerate(ans_matches):
        end = ans_matches[idx + 1].start() if idx + 1 < len(ans_matches) else len(text)
        body = clean_spaces(text[match.end():end])
        for marker in [" This ", " Hopefully", " Yes,", " In this case", " Other books"]:
            pos = body.find(marker)
            if pos > 0:
                body = body[:pos]
        answers[match.group(1)] = body

    overrides = {
        "2.10": "SELECT OrderID, ShipCity + ', ' + ISNULL(ShipRegion, '') + ', ' + ShipCountry AS [Order Address] FROM Orders",
        "2.11": "SELECT OrderID, ShipCity + ', ' + ISNULL(ShipRegion + ', ', '') + ShipCountry AS [Order Address] FROM Orders",
        "2.12": "SELECT OrderID, ShipCity + ', ' + ShipRegion + ', ' + ShipCountry AS [Order Address] FROM Orders",
        "3.7": "SELECT * FROM Products WHERE UnitsInStock * UnitPrice < 100",
        "11.3": "SELECT s.CompanyName, SUM(p.UnitsOnOrder) FROM Products p INNER JOIN Suppliers s ON p.SupplierID = s.SupplierID GROUP BY s.CompanyName",
        "14.2": "SELECT * FROM Shippers CROSS JOIN Customers",
        "14.3": "SELECT COUNT(*) FROM Shippers CROSS JOIN Customers",
    }
    for key, sql in overrides.items():
        answers[key] = sql

    for ex_id in sorted(questions, key=lambda x: tuple(map(int, x.split(".")))):
        if ex_id not in answers:
            continue
        add(
            items,
            questions[ex_id],
            answers[ex_id],
            NORTHWIND_SCHEMA if ex_id != "2.5" else "none",
            f"Learn SQL Fast, Exercise {ex_id}",
        )


def add_silberschatz(items):
    src = "Silberschatz/Korth/Sudarshan, Database System Concepts 7e"
    rows = [
        ("Find titles of courses in the Comp. Sci. department that have 3 credits.",
         "SELECT title FROM course WHERE dept_name = 'Comp. Sci.' AND credits = 3", "Practice Exercise 3.1a"),
        ("Find IDs of all students who were taught by an instructor named Einstein; no duplicates.",
         "SELECT DISTINCT takes.ID FROM takes JOIN teaches USING (course_id, sec_id, semester, year) JOIN instructor ON teaches.ID = instructor.ID WHERE instructor.name = 'Einstein'", "Practice Exercise 3.1b"),
        ("Find the highest salary of any instructor.",
         "SELECT MAX(salary) FROM instructor", "Practice Exercise 3.1c"),
        ("Find all instructors earning the highest salary.",
         "SELECT ID, name, dept_name, salary FROM instructor WHERE salary = (SELECT MAX(salary) FROM instructor)", "Practice Exercise 3.1d"),
        ("Find the enrollment of each section offered in Fall 2017.",
         "SELECT course_id, sec_id, COUNT(ID) AS enrollment FROM takes WHERE semester = 'Fall' AND year = 2017 GROUP BY course_id, sec_id", "Practice Exercise 3.1e"),
        ("Find the maximum enrollment across all sections in Fall 2017.",
         "SELECT MAX(enrollment) FROM (SELECT COUNT(ID) AS enrollment FROM takes WHERE semester = 'Fall' AND year = 2017 GROUP BY course_id, sec_id) AS section_enrollment", "Practice Exercise 3.1f"),
        ("Find the sections that had the maximum enrollment in Fall 2017.",
         "WITH section_enrollment AS (SELECT course_id, sec_id, COUNT(ID) AS enrollment FROM takes WHERE semester = 'Fall' AND year = 2017 GROUP BY course_id, sec_id) SELECT course_id, sec_id, enrollment FROM section_enrollment WHERE enrollment = (SELECT MAX(enrollment) FROM section_enrollment)", "Practice Exercise 3.1g"),
        ("Find total grade points earned by student ID '12345'.",
         "SELECT SUM(course.credits * grade_points.points) FROM takes JOIN course USING (course_id) JOIN grade_points USING (grade) WHERE takes.ID = '12345'", "Practice Exercise 3.2a", "grade_points(grade, points); " + UNIVERSITY_SCHEMA),
        ("Find GPA for student ID '12345'.",
         "SELECT SUM(course.credits * grade_points.points) / SUM(course.credits) AS GPA FROM takes JOIN course USING (course_id) JOIN grade_points USING (grade) WHERE takes.ID = '12345'", "Practice Exercise 3.2b", "grade_points(grade, points); " + UNIVERSITY_SCHEMA),
        ("Find the ID and GPA of each student.",
         "SELECT takes.ID, SUM(course.credits * grade_points.points) / SUM(course.credits) AS GPA FROM takes JOIN course USING (course_id) JOIN grade_points USING (grade) GROUP BY takes.ID", "Practice Exercise 3.2c", "grade_points(grade, points); " + UNIVERSITY_SCHEMA),
        ("Reconsider GPA queries when some grades may be null; find each student's GPA while ignoring null grades.",
         "SELECT takes.ID, SUM(course.credits * grade_points.points) / SUM(course.credits) AS GPA FROM takes JOIN course USING (course_id) JOIN grade_points ON takes.grade = grade_points.grade WHERE takes.grade IS NOT NULL GROUP BY takes.ID", "Practice Exercise 3.2d", "grade_points(grade, points); " + UNIVERSITY_SCHEMA),
        ("Find total number of people who owned cars involved in accidents in 2017.",
         "SELECT COUNT(DISTINCT owns.driver_id) FROM accident JOIN participated USING (report_number) JOIN owns USING (license_plate) WHERE accident.year = 2017", "Practice Exercise 3.4a", "person(driver_id, name, address); car(license_plate, model, year); accident(report_number, year, location); owns(driver_id, license_plate); participated(report_number, license_plate, driver_id, damage_amount)"),
        ("Display the grade for each student based on marks.",
         "SELECT ID, CASE WHEN score < 40 THEN 'F' WHEN score < 60 THEN 'C' WHEN score < 80 THEN 'B' ELSE 'A' END AS grade FROM marks", "Practice Exercise 3.5a", "marks(ID, score)"),
        ("Find the number of students with each grade.",
         "SELECT grade, COUNT(*) FROM (SELECT ID, CASE WHEN score < 40 THEN 'F' WHEN score < 60 THEN 'C' WHEN score < 80 THEN 'B' ELSE 'A' END AS grade FROM marks) AS graded GROUP BY grade", "Practice Exercise 3.5b", "marks(ID, score)"),
        ("Find departments whose names contain 'sci' regardless of case.",
         "SELECT dept_name FROM department WHERE LOWER(dept_name) LIKE '%sci%'", "Practice Exercise 3.6"),
        ("Find IDs of bank customers who have an account but not a loan.",
         "SELECT DISTINCT depositor.ID FROM depositor WHERE depositor.ID NOT IN (SELECT ID FROM borrower)", "Practice Exercise 3.8a", "branch(branch_name, branch_city, assets); customer(ID, customer_name, customer_street, customer_city); loan(loan_number, branch_name, amount); borrower(ID, loan_number); account(account_number, branch_name, balance); depositor(ID, account_number)"),
        ("Find IDs of customers who live on the same street and city as customer '12345'.",
         "SELECT C2.ID FROM customer AS C1 JOIN customer AS C2 ON C1.customer_street = C2.customer_street AND C1.customer_city = C2.customer_city WHERE C1.ID = '12345' AND C2.ID <> '12345'", "Practice Exercise 3.8b", "branch(branch_name, branch_city, assets); customer(ID, customer_name, customer_street, customer_city); loan(loan_number, branch_name, amount); borrower(ID, loan_number); account(account_number, branch_name, balance); depositor(ID, account_number)"),
        ("Find branch names with at least one account customer living in Harrison.",
         "SELECT DISTINCT account.branch_name FROM account JOIN depositor USING (account_number) JOIN customer USING (ID) WHERE customer.customer_city = 'Harrison'", "Practice Exercise 3.8c", "branch(branch_name, branch_city, assets); customer(ID, customer_name, customer_street, customer_city); loan(loan_number, branch_name, amount); borrower(ID, loan_number); account(account_number, branch_name, balance); depositor(ID, account_number)"),
        ("Find ID, name, and city of residence of employees who work for First Bank Corporation.",
         "SELECT employee.ID, person_name, city FROM employee JOIN works USING (ID) WHERE company_name = 'First Bank Corporation'", "Practice Exercise 3.9a", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find ID, name, and city of residence of employees who work for First Bank Corporation and earn more than 10000.",
         "SELECT employee.ID, person_name, city FROM employee JOIN works USING (ID) WHERE company_name = 'First Bank Corporation' AND salary > 10000", "Practice Exercise 3.9b", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find IDs of employees who do not work for First Bank Corporation.",
         "SELECT ID FROM employee EXCEPT SELECT ID FROM works WHERE company_name = 'First Bank Corporation'", "Practice Exercise 3.9c", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find IDs of employees who earn more than every employee of Small Bank Corporation.",
         "SELECT ID FROM works WHERE salary > ALL (SELECT salary FROM works WHERE company_name = 'Small Bank Corporation')", "Practice Exercise 3.9d", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find company names located in every city where Small Bank Corporation is located.",
         "SELECT DISTINCT C.company_name FROM company AS C WHERE NOT EXISTS (SELECT city FROM company WHERE company_name = 'Small Bank Corporation' EXCEPT SELECT city FROM company AS D WHERE D.company_name = C.company_name)", "Practice Exercise 3.9e", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find the company or companies with the most employees.",
         "WITH company_counts AS (SELECT company_name, COUNT(*) AS cnt FROM works GROUP BY company_name) SELECT company_name FROM company_counts WHERE cnt = (SELECT MAX(cnt) FROM company_counts)", "Practice Exercise 3.9f", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find company names whose employees earn a higher average salary than First Bank Corporation.",
         "SELECT company_name FROM works GROUP BY company_name HAVING AVG(salary) > (SELECT AVG(salary) FROM works WHERE company_name = 'First Bank Corporation')", "Practice Exercise 3.9g", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find ID and name of each student who has taken at least one Comp. Sci. course.",
         "SELECT DISTINCT student.ID, student.name FROM student JOIN takes USING (ID) JOIN course USING (course_id) WHERE course.dept_name = 'Comp. Sci.'", "Exercise 3.11a"),
        ("Find ID and name of each student who has not taken any course offered before 2017.",
         "SELECT ID, name FROM student WHERE ID NOT IN (SELECT ID FROM takes WHERE year < 2017)", "Exercise 3.11b"),
        ("For each department, find the maximum salary of instructors.",
         "SELECT dept_name, MAX(salary) FROM instructor GROUP BY dept_name", "Exercise 3.11c"),
        ("Find the lowest across all departments of the per-department maximum instructor salary.",
         "SELECT MIN(max_salary) FROM (SELECT dept_name, MAX(salary) AS max_salary FROM instructor GROUP BY dept_name) AS dept_max", "Exercise 3.11d"),
        ("Find the number of accidents involving a car belonging to John Smith.",
         "SELECT COUNT(DISTINCT accident.report_number) FROM accident JOIN participated USING (report_number) JOIN owns USING (license_plate) JOIN person USING (driver_id) WHERE person.name = 'John Smith'", "Exercise 3.14a", "person(driver_id, name, address); car(license_plate, model, year); accident(report_number, year, location); owns(driver_id, license_plate); participated(report_number, license_plate, driver_id, damage_amount)"),
        ("Find customers who have an account at every branch located in Brooklyn.",
         "SELECT customer.ID, customer.customer_name FROM customer WHERE NOT EXISTS (SELECT branch_name FROM branch WHERE branch_city = 'Brooklyn' EXCEPT SELECT account.branch_name FROM account JOIN depositor USING (account_number) WHERE depositor.ID = customer.ID)", "Exercise 3.15a", "branch(branch_name, branch_city, assets); customer(ID, customer_name, customer_street, customer_city); loan(loan_number, branch_name, amount); borrower(ID, loan_number); account(account_number, branch_name, balance); depositor(ID, account_number)"),
        ("Find the total sum of all loan amounts in the bank.",
         "SELECT SUM(amount) FROM loan", "Exercise 3.15b", "branch(branch_name, branch_city, assets); customer(ID, customer_name, customer_street, customer_city); loan(loan_number, branch_name, amount); borrower(ID, loan_number); account(account_number, branch_name, balance); depositor(ID, account_number)"),
        ("Find branch names with assets greater than those of at least one branch in Brooklyn.",
         "SELECT DISTINCT branch_name FROM branch WHERE assets > SOME (SELECT assets FROM branch WHERE branch_city = 'Brooklyn')", "Exercise 3.15c", "branch(branch_name, branch_city, assets); customer(ID, customer_name, customer_street, customer_city); loan(loan_number, branch_name, amount); borrower(ID, loan_number); account(account_number, branch_name, balance); depositor(ID, account_number)"),
        ("Find ID and name of employees who live in the same city as the company for which they work.",
         "SELECT employee.ID, person_name FROM employee JOIN works USING (ID) JOIN company USING (company_name) WHERE employee.city = company.city", "Exercise 3.16a", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find ID and name of employees who live in the same city and street as their manager.",
         "SELECT E.ID, E.person_name FROM employee AS E JOIN manages AS M ON E.ID = M.ID JOIN employee AS S ON M.manager_id = S.ID WHERE E.city = S.city AND E.street = S.street", "Exercise 3.16b", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find ID and name of employees who earn more than the average salary of their company.",
         "SELECT employee.ID, person_name FROM employee JOIN works AS W USING (ID) WHERE W.salary > (SELECT AVG(salary) FROM works WHERE company_name = W.company_name)", "Exercise 3.16c", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find the company with the smallest payroll.",
         "WITH payroll AS (SELECT company_name, SUM(salary) AS total_payroll FROM works GROUP BY company_name) SELECT company_name FROM payroll WHERE total_payroll = (SELECT MIN(total_payroll) FROM payroll)", "Exercise 3.16d", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Find members who borrowed at least one book published by McGraw-Hill.",
         "SELECT DISTINCT member.memb_no, member.name FROM member JOIN borrowed USING (memb_no) JOIN book USING (isbn) WHERE publisher = 'McGraw-Hill'", "Exercise 3.21a", "member(memb_no, name); book(isbn, title, authors, publisher); borrowed(memb_no, isbn, date)"),
        ("Find members who borrowed every book published by McGraw-Hill.",
         "SELECT member.memb_no, member.name FROM member WHERE NOT EXISTS (SELECT isbn FROM book WHERE publisher = 'McGraw-Hill' EXCEPT SELECT isbn FROM borrowed WHERE borrowed.memb_no = member.memb_no)", "Exercise 3.21b", "member(memb_no, name); book(isbn, title, authors, publisher); borrowed(memb_no, isbn, date)"),
        ("For each publisher, find members who borrowed more than five books of that publisher.",
         "SELECT book.publisher, member.memb_no, member.name FROM member JOIN borrowed USING (memb_no) JOIN book USING (isbn) GROUP BY book.publisher, member.memb_no, member.name HAVING COUNT(*) > 5", "Exercise 3.21c", "member(memb_no, name); book(isbn, title, authors, publisher); borrowed(memb_no, isbn, date)"),
        ("Find the average number of books borrowed per member, counting members with no borrowed books.",
         "SELECT AVG(book_count) FROM (SELECT member.memb_no, COUNT(borrowed.isbn) AS book_count FROM member LEFT JOIN borrowed USING (memb_no) GROUP BY member.memb_no) AS counts", "Exercise 3.21d", "member(memb_no, name); book(isbn, title, authors, publisher); borrowed(memb_no, isbn, date)"),
        ("Rewrite the department total query without using WITH.",
         "SELECT dept_name FROM (SELECT dept_name, SUM(salary) AS value FROM instructor GROUP BY dept_name) AS dept_total, (SELECT AVG(value) AS value FROM (SELECT dept_name, SUM(salary) AS value FROM instructor GROUP BY dept_name) AS dept_total_inner) AS dept_total_avg WHERE dept_total.value >= dept_total_avg.value", "Exercise 3.23"),
        ("Find the name and ID of Accounting students advised by an instructor in Physics.",
         "SELECT student.name, student.ID FROM student JOIN advisor ON student.ID = advisor.s_ID JOIN instructor ON advisor.i_ID = instructor.ID WHERE student.dept_name = 'Accounting' AND instructor.dept_name = 'Physics'", "Exercise 3.24"),
        ("Find departments whose budget is higher than Philosophy, sorted alphabetically.",
         "SELECT dept_name FROM department WHERE budget > (SELECT budget FROM department WHERE dept_name = 'Philosophy') ORDER BY dept_name", "Exercise 3.25"),
        ("For each student who has retaken a course at least twice, show course ID and student ID.",
         "SELECT course_id, ID FROM takes GROUP BY course_id, ID HAVING COUNT(*) >= 3 ORDER BY course_id", "Exercise 3.26"),
        ("Find IDs of students who retook at least three distinct courses at least once.",
         "SELECT ID FROM (SELECT ID, course_id FROM takes GROUP BY ID, course_id HAVING COUNT(*) >= 2) AS retaken GROUP BY ID HAVING COUNT(*) >= 3", "Exercise 3.27"),
        ("Find instructors who teach every course taught in their department.",
         "SELECT I.name, I.ID FROM instructor AS I WHERE NOT EXISTS (SELECT course_id FROM course WHERE dept_name = I.dept_name EXCEPT SELECT teaches.course_id FROM teaches WHERE teaches.ID = I.ID) ORDER BY I.name", "Exercise 3.28"),
        ("Find History students whose name begins with D and who have not taken at least five Music courses.",
         "SELECT student.name, student.ID FROM student WHERE dept_name = 'History' AND name LIKE 'D%' AND 5 > (SELECT COUNT(DISTINCT takes.course_id) FROM takes JOIN course USING (course_id) WHERE takes.ID = student.ID AND course.dept_name = 'Music')", "Exercise 3.29"),
        ("Find instructors who have never given an A grade.",
         "SELECT ID, name FROM instructor WHERE NOT EXISTS (SELECT * FROM teaches JOIN takes USING (course_id, sec_id, semester, year) WHERE teaches.ID = instructor.ID AND takes.grade = 'A')", "Exercise 3.31"),
        ("Find instructors who have never given an A grade but have given at least one non-null non-A grade.",
         "SELECT ID, name FROM instructor WHERE NOT EXISTS (SELECT * FROM teaches JOIN takes USING (course_id, sec_id, semester, year) WHERE teaches.ID = instructor.ID AND takes.grade = 'A') AND EXISTS (SELECT * FROM teaches JOIN takes USING (course_id, sec_id, semester, year) WHERE teaches.ID = instructor.ID AND takes.grade IS NOT NULL AND takes.grade <> 'A')", "Exercise 3.32"),
        ("Find Comp. Sci. courses that had at least one section ending at or after 12:00.",
         "SELECT DISTINCT course.course_id, title FROM course JOIN section USING (course_id) JOIN time_slot USING (time_slot_id) WHERE course.dept_name = 'Comp. Sci.' AND (end_hr > 12 OR (end_hr = 12 AND end_min >= 0))", "Exercise 3.33"),
        ("Find the number of students in each section.",
         "SELECT course_id, sec_id, year, semester, COUNT(ID) AS num FROM takes GROUP BY course_id, sec_id, year, semester", "Exercise 3.34"),
        ("Find sections with maximum enrollment.",
         "WITH section_counts AS (SELECT course_id, sec_id, year, semester, COUNT(ID) AS num FROM takes GROUP BY course_id, sec_id, year, semester) SELECT course_id, sec_id, year, semester, num FROM section_counts WHERE num = (SELECT MAX(num) FROM section_counts)", "Exercise 3.35"),
        ("Display all instructors with ID and number of sections taught, including zero, using outer join.",
         "SELECT instructor.ID, COUNT(teaches.course_id) AS section_count FROM instructor LEFT JOIN teaches ON instructor.ID = teaches.ID GROUP BY instructor.ID", "Practice Exercise 4.2a"),
        ("Display all instructors with ID and number of sections taught using a scalar subquery.",
         "SELECT ID, (SELECT COUNT(*) FROM teaches WHERE teaches.ID = instructor.ID) AS section_count FROM instructor", "Practice Exercise 4.2b"),
        ("Display all Spring 2018 sections with instructor ID and name, preserving sections without instructors.",
         "SELECT section.course_id, section.sec_id, section.semester, section.year, teaches.ID, COALESCE(instructor.name, '-') AS name FROM section LEFT JOIN teaches USING (course_id, sec_id, semester, year) LEFT JOIN instructor ON teaches.ID = instructor.ID WHERE section.semester = 'Spring' AND section.year = 2018", "Practice Exercise 4.2c"),
        ("Display all departments with total number of instructors, including zero, without subqueries.",
         "SELECT department.dept_name, COUNT(instructor.ID) AS instructor_count FROM department LEFT JOIN instructor USING (dept_name) GROUP BY department.dept_name", "Practice Exercise 4.2d"),
        ("Define the view student_grades(ID, GPA) that handles null grades.",
         "CREATE VIEW student_grades(ID, GPA) AS SELECT takes.ID, SUM(course.credits * grade_points.points) / SUM(course.credits) AS GPA FROM takes JOIN course USING (course_id) LEFT JOIN grade_points USING (grade) WHERE takes.grade IS NOT NULL GROUP BY takes.ID", "Practice Exercise 4.6", "grade_points(grade, points); " + UNIVERSITY_SCHEMA),
        ("Find instructor-section combinations that violate the same time slot in different classrooms constraint.",
         "SELECT T1.ID, T1.course_id, T1.sec_id, T1.semester, T1.year FROM teaches AS T1 JOIN section AS S1 USING (course_id, sec_id, semester, year) JOIN teaches AS T2 ON T1.ID = T2.ID JOIN section AS S2 ON T2.course_id = S2.course_id AND T2.sec_id = S2.sec_id AND T2.semester = S2.semester AND T2.year = S2.year WHERE T1.semester = T2.semester AND T1.year = T2.year AND S1.time_slot_id = S2.time_slot_id AND (S1.building <> S2.building OR S1.room_number <> S2.room_number) AND (T1.course_id, T1.sec_id) <> (T2.course_id, T2.sec_id)", "Practice Exercise 4.8a"),
        ("Express a natural full outer join of a(name, address, title) and b(name, address, salary) using full outer join with ON and coalesce.",
         "SELECT COALESCE(a.name, b.name) AS name, COALESCE(a.address, b.address) AS address, a.title, b.salary FROM a FULL OUTER JOIN b ON a.name = b.name AND a.address = b.address", "Practice Exercise 4.10", "a(name, address, title); b(name, address, salary)"),
        ("Rewrite section natural join classroom using inner join with USING.",
         "SELECT * FROM section INNER JOIN classroom USING (building, room_number)", "Exercise 4.15"),
        ("Find IDs of students who have never taken a course using outer join.",
         "SELECT student.ID FROM student LEFT JOIN takes USING (ID) WHERE takes.ID IS NULL", "Exercise 4.16"),
        ("Express students with no non-null advisor using no subqueries and no set operations.",
         "SELECT student.ID FROM student LEFT JOIN advisor ON student.ID = advisor.s_ID AND advisor.i_ID IS NOT NULL WHERE advisor.s_ID IS NULL", "Exercise 4.17"),
        ("Find employees with no manager using outer join.",
         "SELECT E.ID FROM employee AS E LEFT JOIN manages AS M ON E.ID = M.ID WHERE M.manager_id IS NULL", "Exercise 4.18", "employee(ID, person_name, street, city); works(ID, company_name, salary); company(company_name, city); manages(ID, manager_id)"),
        ("Define a view tot_credits(year, num_credits) giving total credits taken each year.",
         "CREATE VIEW tot_credits(year, num_credits) AS SELECT takes.year, SUM(course.credits) FROM takes JOIN course USING (course_id) GROUP BY takes.year", "Exercise 4.20"),
        ("Express the coalesce function using a CASE construct.",
         "SELECT CASE WHEN x IS NOT NULL THEN x ELSE y END AS coalesced_value FROM values_source", "Exercise 4.22", "values_source(x, y)"),
        ("Find top 10 students by total marks using SQL ranking and include ties.",
         "SELECT student, total_marks FROM (SELECT student, SUM(marks) AS total_marks, RANK() OVER (ORDER BY SUM(marks) DESC) AS rnk FROM S GROUP BY student) AS ranked WHERE rnk <= 10", "Practice Exercise 5.8", "S(student, subject, marks)"),
        ("List each NYSE trading day by number of shares traded and show rank.",
         "SELECT year, month, day, shares_traded, RANK() OVER (ORDER BY shares_traded DESC) AS rank FROM nyse", "Practice Exercise 5.9", "nyse(year, month, day, shares_traded, dollar_volume)"),
        ("Generate report of shares traded, number of trades, and total dollar volume by year, month, and day.",
         "SELECT year, month, day, SUM(shares_traded) AS shares_traded, COUNT(*) AS num_trades, SUM(dollar_volume) AS dollar_volume FROM nyse GROUP BY ROLLUP(year, month, day)", "Practice Exercise 5.10", "nyse(year, month, day, shares_traded, dollar_volume)"),
        ("Express GROUP BY CUBE(a, b, c, d) using ROLLUP in one GROUP BY clause.",
         "SELECT a, b, c, d, SUM(value) FROM r GROUP BY ROLLUP(a), ROLLUP(b), ROLLUP(c), ROLLUP(d)", "Practice Exercise 5.11", "r(a, b, c, d, value)"),
        ("In a Java/JDBC teaching-record program, search instructors whose names match a substring.",
         "SELECT ID, name FROM instructor WHERE name LIKE '%' || :substring || '%'", "Exercise 5.12b"),
        ("In a Java/JDBC teaching-record program, check whether an instructor ID exists.",
         "SELECT COUNT(*) FROM instructor WHERE ID = :instructor_id", "Exercise 5.12c"),
        ("In a Java/JDBC teaching-record program, print the teaching record for an instructor with total enrollment sorted by department, course, year, and semester.",
         "SELECT course.dept_name, course.course_id, course.title, section.sec_id, section.semester, section.year, COUNT(takes.ID) AS total_enrollment FROM teaches JOIN section USING (course_id, sec_id, semester, year) JOIN course USING (course_id) LEFT JOIN takes USING (course_id, sec_id, semester, year) WHERE teaches.ID = :instructor_id GROUP BY course.dept_name, course.course_id, course.title, section.sec_id, section.semester, section.year ORDER BY course.dept_name, course.course_id, section.year, section.semester", "Exercise 5.12d"),
        ("Find companies whose average salary is higher than First Bank using avg_salary function.",
         "SELECT company_name FROM works GROUP BY company_name HAVING AVG(salary) > avg_salary('First Bank')", "Exercise 5.15", "employee(employee_name, street, city); works(employee_name, company_name, salary)"),
        ("Write a recursive SQL query that outputs names of all subparts of part P-100.",
         "WITH RECURSIVE all_subparts(part_id, subpart_id) AS (SELECT part_id, subpart_id FROM subpart WHERE part_id = 'P-100' UNION ALL SELECT s.part_id, s.subpart_id FROM subpart AS s JOIN all_subparts AS a ON s.part_id = a.subpart_id) SELECT DISTINCT part.name FROM all_subparts JOIN part ON all_subparts.subpart_id = part.part_id", "Exercise 5.16", "part(part_id, name, cost); subpart(part_id, subpart_id, count)"),
        ("Modify the recursive prerequisite query to include depth.",
         "WITH RECURSIVE prereq_depth(course_id, prereq_id, depth) AS (SELECT course_id, prereq_id, 0 FROM prereq UNION ALL SELECT p.course_id, r.prereq_id, p.depth + 1 FROM prereq_depth AS p JOIN prereq AS r ON p.prereq_id = r.course_id) SELECT * FROM prereq_depth", "Exercise 5.21"),
        ("Generate a histogram showing sum of c versus a using 20 equal-sized partitions.",
         "SELECT width_bucket(a, (SELECT MIN(a) FROM s), (SELECT MAX(a) FROM s), 20) AS bucket, SUM(c) FROM s GROUP BY bucket ORDER BY bucket", "Exercise 5.22", "s(a, b, c)"),
        ("For each NYSE month, show total monthly dollar volume and average over that month and the two prior months.",
         "WITH monthly AS (SELECT year, month, SUM(dollar_volume) AS total_volume FROM nyse GROUP BY year, month) SELECT year, month, total_volume, AVG(total_volume) OVER (ORDER BY year, month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS moving_avg FROM monthly", "Exercise 5.23", "nyse(year, month, day, shares_traded, dollar_volume)"),
        ("Give the result of grouping relation r by rollup(building, room_number, time_slot_id).",
         "SELECT building, room_number, time_slot_id, COUNT(*) FROM r GROUP BY ROLLUP(building, room_number, time_slot_id)", "Exercise 5.24", "r(building, room_number, time_slot_id, course_id, sec_id)"),
        ("Given relation r(A, B, C), write an SQL query to test whether functional dependency B -> C holds; returning rows means the dependency is violated.",
         "SELECT B FROM r GROUP BY B HAVING COUNT(DISTINCT C) > 1", "Exercise 7.9", "r(A, B, C)"),
        ("Compute the temporal natural join of r(A, B, validtime) and s(B, C, validtime).",
         "SELECT r.A, r.B, s.C, r.validtime * s.validtime AS validtime FROM r JOIN s ON r.B = s.B AND r.validtime && s.validtime", "Exercise 7.44", "r(A, B, validtime); s(B, C, validtime)"),
        ("Using the employee schema with references, find the company with the most employees.",
         "WITH company_counts AS (SELECT company_name, COUNT(*) AS cnt FROM works GROUP BY company_name) SELECT company_name FROM company_counts WHERE cnt = (SELECT MAX(cnt) FROM company_counts)", "Exercise 8.6b.i", "employee(person_name, street, city); works(person_name, company_name, salary); company(company_name, city); manages(person_name, manager_name)"),
        ("Using the employee schema with references, find the company with the smallest payroll.",
         "WITH payroll AS (SELECT company_name, SUM(salary) AS total_payroll FROM works GROUP BY company_name) SELECT company_name FROM payroll WHERE total_payroll = (SELECT MIN(total_payroll) FROM payroll)", "Exercise 8.6b.ii", "employee(person_name, street, city); works(person_name, company_name, salary); company(company_name, city); manages(person_name, manager_name)"),
        ("Using the employee schema with references, find companies whose employees earn a higher average salary than First Bank Corporation.",
         "SELECT company_name FROM works GROUP BY company_name HAVING AVG(salary) > (SELECT AVG(salary) FROM works WHERE company_name = 'First Bank Corporation')", "Exercise 8.6b.iii", "employee(person_name, street, city); works(person_name, company_name, salary); company(company_name, city); manages(person_name, manager_name)"),
        ("Represent PageRank matrices as relations and write an SQL query that implements one iterative PageRank step.",
         "SELECT links.target AS page, SUM(rank.score / out_degree.degree) AS score FROM rank JOIN links ON rank.page = links.source JOIN out_degree ON links.source = out_degree.source GROUP BY links.target", "Exercise 8.8", "rank(page, score); links(source, target); out_degree(source, degree)"),
        ("Using PostGIS, find names of students whose location is within classroom Packard 101.",
         "SELECT student.name FROM student JOIN classroom ON ST_Within(student.location, classroom.location) WHERE classroom.building = 'Packard' AND classroom.room_number = '101'", "Exercise 8.9a", "student(ID, name, location point); classroom(building, room_number, location polygon)"),
        ("Using PostGIS, find all classrooms within 100 meters of Packard 101.",
         "SELECT C2.building, C2.room_number FROM classroom AS C1 JOIN classroom AS C2 ON ST_DWithin(C1.location, C2.location, 100) WHERE C1.building = 'Packard' AND C1.room_number = '101' AND (C2.building <> C1.building OR C2.room_number <> C1.room_number)", "Exercise 8.9b", "student(ID, name, location point); classroom(building, room_number, location polygon)"),
        ("Using PostGIS, find the ID and name of the student geographically nearest to student ID 12345.",
         "SELECT S2.ID, S2.name FROM student AS S1 JOIN student AS S2 ON S1.ID <> S2.ID WHERE S1.ID = '12345' ORDER BY ST_Distance(S1.location, S2.location) LIMIT 1", "Exercise 8.9c", "student(ID, name, location point); classroom(building, room_number, location polygon)"),
        ("Using PostGIS, find ID and names of all pairs of students whose locations are less than 200 meters apart.",
         "SELECT S1.ID, S1.name, S2.ID, S2.name FROM student AS S1 JOIN student AS S2 ON S1.ID < S2.ID WHERE ST_DWithin(S1.location, S2.location, 200)", "Exercise 8.9d", "student(ID, name, location point); classroom(building, room_number, location polygon)"),
        ("Create a query with an equality condition on student.name for query plan inspection.",
         "SELECT * FROM student WHERE name = 'Zhang'", "Practice Exercise 16.1a"),
        ("Create a simple query joining two or three relations for query plan inspection.",
         "SELECT student.ID, student.name, takes.course_id FROM student JOIN takes USING (ID)", "Practice Exercise 16.1c"),
        ("Create a query that computes an aggregate with grouping for query plan inspection.",
         "SELECT dept_name, COUNT(*) FROM student GROUP BY dept_name", "Practice Exercise 16.1d"),
        ("Create an SQL query whose chosen plan uses a semijoin operation.",
         "SELECT course_id FROM course WHERE EXISTS (SELECT 1 FROM section WHERE section.course_id = course.course_id)", "Practice Exercise 16.1e"),
        ("Create an SQL query that uses NOT IN with a subquery using aggregation.",
         "SELECT dept_name FROM department WHERE dept_name NOT IN (SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) > 70000)", "Practice Exercise 16.1f"),
        ("Create a query likely to use correlated evaluation.",
         "SELECT name FROM instructor AS I WHERE salary > (SELECT AVG(salary) FROM instructor WHERE dept_name = I.dept_name)", "Practice Exercise 16.1g"),
        ("On the bank database, write a nested query to find, for each branch whose name starts with B, accounts with the maximum balance at that branch.",
         "SELECT account_number, branch_name, balance FROM account AS A WHERE branch_name LIKE 'B%' AND balance = (SELECT MAX(balance) FROM account WHERE branch_name = A.branch_name)", "Exercise 16.15a", "branch(branch_name, branch_city, assets); customer(customer_name, customer_street, customer_city); loan(loan_number, branch_name, amount); borrower(customer_name, loan_number); account(account_number, branch_name, balance); depositor(customer_name, account_number)"),
        ("Rewrite the branch maximum-balance account query without a nested subquery.",
         "SELECT A.account_number, A.branch_name, A.balance FROM account AS A LEFT JOIN account AS B ON A.branch_name = B.branch_name AND A.balance < B.balance WHERE A.branch_name LIKE 'B%' AND B.account_number IS NULL", "Exercise 16.15b", "branch(branch_name, branch_city, assets); customer(customer_name, customer_street, customer_city); loan(loan_number, branch_name, amount); borrower(customer_name, loan_number); account(account_number, branch_name, balance); depositor(customer_name, account_number)"),
    ]
    for row in rows:
        q, ans, ex = row[:3]
        schema = row[3] if len(row) > 3 else UNIVERSITY_SCHEMA
        add(items, q, ans, schema, f"{src}, {ex}")


def add_elmasri(items):
    src = "Elmasri/Navathe, Fundamentals of Database Systems 7e"
    rows = [
        ("Retrieve birth date and address of employees named John B. Smith.",
         "SELECT Bdate, Address FROM EMPLOYEE WHERE Fname = 'John' AND Minit = 'B' AND Lname = 'Smith'", "Query 0"),
        ("Retrieve name and address of employees who work for the Research department.",
         "SELECT Fname, Lname, Address FROM EMPLOYEE, DEPARTMENT WHERE Dname = 'Research' AND Dnumber = Dno", "Query 1"),
        ("For every project located in Stafford, list project number, controlling department number, and manager last name, birth date, and address.",
         "SELECT Pnumber, Dnum, Lname, Bdate, Address FROM PROJECT, DEPARTMENT, EMPLOYEE WHERE Dnum = Dnumber AND Mgr_ssn = Ssn AND Plocation = 'Stafford'", "Query 2"),
        ("For each employee, retrieve the employee first and last name and the first and last name of their supervisor.",
         "SELECT E.Fname, E.Lname, S.Fname, S.Lname FROM EMPLOYEE AS E, EMPLOYEE AS S WHERE E.Super_ssn = S.Ssn", "Query 8"),
        ("Select all employee SSNs.",
         "SELECT Ssn FROM EMPLOYEE", "Query 9"),
        ("Select all combinations of employee SSN and department name.",
         "SELECT Ssn, Dname FROM EMPLOYEE, DEPARTMENT", "Query 10"),
        ("Retrieve all employees who work in department number 5.",
         "SELECT * FROM EMPLOYEE WHERE Dno = 5", "Query 1C"),
        ("Retrieve salary of every employee.",
         "SELECT ALL Salary FROM EMPLOYEE", "Query 11"),
        ("Retrieve all distinct salary values.",
         "SELECT DISTINCT Salary FROM EMPLOYEE", "Query 11A"),
        ("Make a list of all project numbers for projects involving an employee whose last name is Smith, either as worker or department manager.",
         "(SELECT DISTINCT Pnumber FROM PROJECT, DEPARTMENT, EMPLOYEE WHERE Dnum = Dnumber AND Mgr_ssn = Ssn AND Lname = 'Smith') UNION (SELECT DISTINCT Pnumber FROM PROJECT, WORKS_ON, EMPLOYEE WHERE Pnumber = Pno AND Essn = Ssn AND Lname = 'Smith')", "Query 4"),
        ("Retrieve all employees whose address is in Houston, Texas.",
         "SELECT Fname, Lname FROM EMPLOYEE WHERE Address LIKE '%Houston,TX%'", "Query 12"),
        ("Retrieve all employees in department 5 whose salary is between 30000 and 40000.",
         "SELECT * FROM EMPLOYEE WHERE (Salary BETWEEN 30000 AND 40000) AND Dno = 5", "Query 14"),
        ("Retrieve names of employees who have no dependents.",
         "SELECT Fname, Lname FROM EMPLOYEE WHERE NOT EXISTS (SELECT * FROM DEPENDENT WHERE Ssn = Essn)", "Query 6"),
        ("Retrieve managers who have at least one dependent.",
         "SELECT Fname, Lname FROM EMPLOYEE WHERE EXISTS (SELECT * FROM DEPENDENT WHERE Ssn = Essn) AND EXISTS (SELECT * FROM DEPARTMENT WHERE Ssn = Mgr_ssn)", "Query 7"),
        ("Retrieve each employee name and supervisor name using left outer join.",
         "SELECT E.Lname AS Employee_name, S.Lname AS Supervisor_name FROM EMPLOYEE AS E LEFT OUTER JOIN EMPLOYEE AS S ON E.Super_ssn = S.Ssn", "Query 8A"),
        ("Retrieve the sum of all employee salaries, maximum salary, minimum salary, and average salary.",
         "SELECT SUM(Salary), MAX(Salary), MIN(Salary), AVG(Salary) FROM EMPLOYEE", "Query 19"),
        ("Retrieve the number of employees in the company.",
         "SELECT COUNT(*) FROM EMPLOYEE", "Query 20"),
        ("Retrieve the number of employees in department 5.",
         "SELECT COUNT(*) FROM EMPLOYEE WHERE Dno = 5", "Query 21"),
        ("For each department, retrieve department number, number of employees, and average salary.",
         "SELECT Dno, COUNT(*), AVG(Salary) FROM EMPLOYEE GROUP BY Dno", "Query 24"),
        ("For each project, retrieve project number, project name, and number of employees who work on it.",
         "SELECT Pnumber, Pname, COUNT(*) FROM PROJECT, WORKS_ON WHERE Pnumber = Pno GROUP BY Pnumber, Pname", "Query 25"),
        ("For each project with more than two employees, retrieve project number, project name, and employee count.",
         "SELECT Pnumber, Pname, COUNT(*) FROM PROJECT, WORKS_ON WHERE Pnumber = Pno GROUP BY Pnumber, Pname HAVING COUNT(*) > 2", "Query 26"),
        ("For each department with more than five employees, retrieve department number and number of employees making more than 40000.",
         "SELECT Dno, COUNT(*) FROM EMPLOYEE WHERE Salary > 40000 AND Dno IN (SELECT Dno FROM EMPLOYEE GROUP BY Dno HAVING COUNT(*) > 5) GROUP BY Dno", "Query 28"),
        ("Retrieve names of department 5 employees who work more than 10 hours per week on ProductX.",
         "SELECT Fname, Lname FROM EMPLOYEE, WORKS_ON, PROJECT WHERE Ssn = Essn AND Pno = Pnumber AND Dno = 5 AND Hours > 10 AND Pname = 'ProductX'", "Exercise 6.10a"),
        ("List names of employees who have a dependent with the same first name as themselves.",
         "SELECT Fname, Lname FROM EMPLOYEE WHERE EXISTS (SELECT * FROM DEPENDENT WHERE Ssn = Essn AND Fname = Dependent_name)", "Exercise 6.10b"),
        ("Find names of employees directly supervised by Franklin Wong.",
         "SELECT E.Fname, E.Lname FROM EMPLOYEE AS E, EMPLOYEE AS S WHERE E.Super_ssn = S.Ssn AND S.Fname = 'Franklin' AND S.Lname = 'Wong'", "Exercise 6.10c"),
        ("Retrieve names of all senior students majoring in CS.",
         "SELECT Name FROM STUDENT WHERE Class = 4 AND Major = 'cs'", "Exercise 6.12a", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("Retrieve names of all courses taught by Professor King in 2007 and 2008.",
         "SELECT DISTINCT C.Course_name FROM COURSE AS C JOIN SECTION AS S ON C.Course_number = S.Course_number WHERE S.Instructor = 'King' AND S.Year IN (2007, 2008)", "Exercise 6.12b", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("For each section taught by Professor King, retrieve course number, semester, year, and number of students.",
         "SELECT S.Course_number, S.Semester, S.Year, COUNT(G.Student_number) FROM SECTION AS S LEFT JOIN GRADE_REPORT AS G ON S.Section_identifier = G.Section_identifier WHERE S.Instructor = 'King' GROUP BY S.Course_number, S.Semester, S.Year", "Exercise 6.12c", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("Retrieve transcript of each senior CS student.",
         "SELECT ST.Name, C.Course_name, C.Course_number, C.Credit_hours, S.Semester, S.Year, G.Grade FROM STUDENT AS ST JOIN GRADE_REPORT AS G ON ST.Student_number = G.Student_number JOIN SECTION AS S ON G.Section_identifier = S.Section_identifier JOIN COURSE AS C ON S.Course_number = C.Course_number WHERE ST.Class = 4 AND ST.Major = 'CS'", "Exercise 6.12d", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("For each department whose average salary is more than 30000, retrieve department name and number of employees.",
         "SELECT Dname, COUNT(*) FROM DEPARTMENT JOIN EMPLOYEE ON Dnumber = Dno GROUP BY Dname HAVING AVG(Salary) > 30000", "Exercise 7.5a"),
        ("Retrieve names and major departments of all straight-A students.",
         "SELECT Name, Major FROM STUDENT AS S WHERE NOT EXISTS (SELECT * FROM GRADE_REPORT AS G WHERE G.Student_number = S.Student_number AND G.Grade <> 'A')", "Exercise 7.6a", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("Retrieve names and major departments of students who do not have grade A in any course.",
         "SELECT Name, Major FROM STUDENT AS S WHERE NOT EXISTS (SELECT * FROM GRADE_REPORT AS G WHERE G.Student_number = S.Student_number AND G.Grade = 'A')", "Exercise 7.6b", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("Retrieve names of employees who work in the department that has the highest-paid employee.",
         "SELECT Fname, Lname FROM EMPLOYEE WHERE Dno = (SELECT Dno FROM EMPLOYEE WHERE Salary = (SELECT MAX(Salary) FROM EMPLOYEE))", "Exercise 7.7a"),
        ("Retrieve names of employees whose supervisor's supervisor has SSN 888665555.",
         "SELECT E.Fname, E.Lname FROM EMPLOYEE AS E JOIN EMPLOYEE AS S ON E.Super_ssn = S.Ssn WHERE S.Super_ssn = '888665555'", "Exercise 7.7b"),
        ("Retrieve names of employees who make at least 10000 more than the lowest-paid employee.",
         "SELECT Fname, Lname FROM EMPLOYEE WHERE Salary >= (SELECT MIN(Salary) FROM EMPLOYEE) + 10000", "Exercise 7.7c"),
        ("For the DEPT_SUMMARY view, retrieve all rows.",
         "SELECT * FROM DEPT_SUMMARY", "Exercise 7.9a", "DEPT_SUMMARY(D, C, Total_s, Average_s); EMPLOYEE(Fname, Minit, Lname, Ssn, Bdate, Address, Sex, Salary, Super_ssn, Dno)"),
        ("For the DEPT_SUMMARY view, retrieve department and count where total salary is greater than 100000.",
         "SELECT D, C FROM DEPT_SUMMARY WHERE Total_s > 100000", "Exercise 7.9b", "DEPT_SUMMARY(D, C, Total_s, Average_s); EMPLOYEE(Fname, Minit, Lname, Ssn, Bdate, Address, Sex, Salary, Super_ssn, Dno)"),
        ("For the DEPT_SUMMARY view, retrieve department and average salary where the department count exceeds department 4's count.",
         "SELECT D, Average_s FROM DEPT_SUMMARY WHERE C > (SELECT C FROM DEPT_SUMMARY WHERE D = 4)", "Exercise 7.9c", "DEPT_SUMMARY(D, C, Total_s, Average_s); EMPLOYEE(Fname, Minit, Lname, Ssn, Bdate, Address, Sex, Salary, Super_ssn, Dno)"),
        ("In embedded SQL with C, read a student's name and print the student's grade point average.",
         "SELECT AVG(CASE Grade WHEN 'A' THEN 4 WHEN 'B' THEN 3 WHEN 'C' THEN 2 WHEN 'D' THEN 1 ELSE 0 END) AS GPA FROM STUDENT JOIN GRADE_REPORT USING (Student_number) WHERE Name = :student_name", "Exercise 10.7", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("In SQLJ with Java, read a student's name and print the student's grade point average.",
         "SELECT AVG(CASE Grade WHEN 'A' THEN 4 WHEN 'B' THEN 3 WHEN 'C' THEN 2 WHEN 'D' THEN 1 ELSE 0 END) AS GPA FROM STUDENT JOIN GRADE_REPORT USING (Student_number) WHERE Name = :student_name", "Exercise 10.8", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("In embedded SQL with C, retrieve books that became overdue yesterday and print book title and borrower name.",
         "SELECT BOOK.Title, BORROWER.Name FROM BOOK JOIN BOOK_LOANS USING (Book_id) JOIN BORROWER USING (Card_no) WHERE BOOK_LOANS.Due_date = CURRENT_DATE - INTERVAL '1' DAY", "Exercise 10.9", "BOOK(Book_id, Title, Publisher_name); BOOK_AUTHORS(Book_id, Author_name); PUBLISHER(Name, Address, Phone); BOOK_COPIES(Book_id, Branch_id, No_of_copies); LIBRARY_BRANCH(Branch_id, Branch_name, Address); BOOK_LOANS(Book_id, Branch_id, Card_no, Date_out, Due_date); BORROWER(Card_no, Name, Address, Phone)"),
        ("In SQLJ with Java, retrieve books that became overdue yesterday and print book title and borrower name.",
         "SELECT BOOK.Title, BORROWER.Name FROM BOOK JOIN BOOK_LOANS USING (Book_id) JOIN BORROWER USING (Card_no) WHERE BOOK_LOANS.Due_date = CURRENT_DATE - INTERVAL '1' DAY", "Exercise 10.10", "BOOK(Book_id, Title, Publisher_name); BOOK_AUTHORS(Book_id, Author_name); PUBLISHER(Name, Address, Phone); BOOK_COPIES(Book_id, Branch_id, No_of_copies); LIBRARY_BRANCH(Branch_id, Branch_name, Address); BOOK_LOANS(Book_id, Branch_id, Card_no, Date_out, Due_date); BORROWER(Card_no, Name, Address, Phone)"),
        ("Using SQL/CLI with C, read a student's name and print the student's grade point average.",
         "SELECT AVG(CASE Grade WHEN 'A' THEN 4 WHEN 'B' THEN 3 WHEN 'C' THEN 2 WHEN 'D' THEN 1 ELSE 0 END) AS GPA FROM STUDENT JOIN GRADE_REPORT USING (Student_number) WHERE Name = :student_name", "Exercise 10.11a", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("Using SQL/CLI with C, retrieve books that became overdue yesterday and print book title and borrower name.",
         "SELECT BOOK.Title, BORROWER.Name FROM BOOK JOIN BOOK_LOANS USING (Book_id) JOIN BORROWER USING (Card_no) WHERE BOOK_LOANS.Due_date = CURRENT_DATE - INTERVAL '1' DAY", "Exercise 10.11b", "BOOK(Book_id, Title, Publisher_name); BOOK_AUTHORS(Book_id, Author_name); PUBLISHER(Name, Address, Phone); BOOK_COPIES(Book_id, Branch_id, No_of_copies); LIBRARY_BRANCH(Branch_id, Branch_name, Address); BOOK_LOANS(Book_id, Branch_id, Card_no, Date_out, Due_date); BORROWER(Card_no, Name, Address, Phone)"),
        ("Using JDBC with Java, read a student's name and print the student's grade point average.",
         "SELECT AVG(CASE Grade WHEN 'A' THEN 4 WHEN 'B' THEN 3 WHEN 'C' THEN 2 WHEN 'D' THEN 1 ELSE 0 END) AS GPA FROM STUDENT JOIN GRADE_REPORT USING (Student_number) WHERE Name = :student_name", "Exercise 10.12a", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("Using JDBC with Java, retrieve books that became overdue yesterday and print book title and borrower name.",
         "SELECT BOOK.Title, BORROWER.Name FROM BOOK JOIN BOOK_LOANS USING (Book_id) JOIN BORROWER USING (Card_no) WHERE BOOK_LOANS.Due_date = CURRENT_DATE - INTERVAL '1' DAY", "Exercise 10.12b", "BOOK(Book_id, Title, Publisher_name); BOOK_AUTHORS(Book_id, Author_name); PUBLISHER(Name, Address, Phone); BOOK_COPIES(Book_id, Branch_id, No_of_copies); LIBRARY_BRANCH(Branch_id, Branch_name, Address); BOOK_LOANS(Book_id, Branch_id, Card_no, Date_out, Due_date); BORROWER(Card_no, Name, Address, Phone)"),
        ("Write an SQL/PSM function core query to compute a student's grade point average by name.",
         "SELECT AVG(CASE Grade WHEN 'A' THEN 4 WHEN 'B' THEN 3 WHEN 'C' THEN 2 WHEN 'D' THEN 1 ELSE 0 END) AS GPA FROM STUDENT JOIN GRADE_REPORT USING (Student_number) WHERE Name = student_name", "Exercise 10.13", "STUDENT(Name, Student_number, Class, Major); COURSE(Course_name, Course_number, Credit_hours, Department); SECTION(Section_identifier, Course_number, Semester, Year, Instructor); GRADE_REPORT(Student_number, Section_identifier, Grade); PREREQUISITE(Course_number, Prerequisite_number)"),
        ("Create a view with department name, manager name, and manager salary for every department.",
         "CREATE VIEW DEPT_MGR AS SELECT D.Dname, E.Fname, E.Lname, E.Salary FROM DEPARTMENT AS D JOIN EMPLOYEE AS E ON D.Mgr_ssn = E.Ssn", "Exercise 7.8a"),
        ("Create a view with employee name, supervisor name, and employee salary for employees in Research.",
         "CREATE VIEW RESEARCH_EMP_SUP AS SELECT E.Fname AS Emp_fname, E.Lname AS Emp_lname, S.Fname AS Sup_fname, S.Lname AS Sup_lname, E.Salary FROM EMPLOYEE AS E LEFT JOIN EMPLOYEE AS S ON E.Super_ssn = S.Ssn JOIN DEPARTMENT AS D ON E.Dno = D.Dnumber WHERE D.Dname = 'Research'", "Exercise 7.8b"),
        ("Create a view with project name, controlling department name, number of employees, and total hours per week for each project.",
         "CREATE VIEW PROJECT_SUMMARY AS SELECT P.Pname, D.Dname, COUNT(W.Essn) AS num_employees, SUM(W.Hours) AS total_hours FROM PROJECT AS P JOIN DEPARTMENT AS D ON P.Dnum = D.Dnumber LEFT JOIN WORKS_ON AS W ON P.Pnumber = W.Pno GROUP BY P.Pname, D.Dname", "Exercise 7.8c"),
        ("Create a view with project summary for projects with more than one employee.",
         "CREATE VIEW PROJECT_SUMMARY_GT1 AS SELECT P.Pname, D.Dname, COUNT(W.Essn) AS num_employees, SUM(W.Hours) AS total_hours FROM PROJECT AS P JOIN DEPARTMENT AS D ON P.Dnum = D.Dnumber JOIN WORKS_ON AS W ON P.Pnumber = W.Pno GROUP BY P.Pname, D.Dname HAVING COUNT(W.Essn) > 1", "Exercise 7.8d"),
        ("For a distributed bookstore database, write the remote SQL subqueries generated by a query for books priced between 15 and 55 submitted at EAST.",
         "SELECT Book_no, Total_stock FROM BOOK2 WHERE price > 20 AND price <= 50 UNION ALL SELECT Book_no, Total_stock FROM BOOK3 WHERE price > 50 AND price < 55", "Exercise 23.29a", "BOOKS(Book_no, Title, Author, price, Total_stock); BOOK1(Book_no, Title, Author, price, Total_stock); BOOK2(Book_no, Title, Author, price, Total_stock); BOOK3(Book_no, Title, Author, price, Total_stock); BOOK4(Book_no, Title, Author, price, Total_stock); BOOKSTORE(Store_no, Zip); STOCK(Store_no, Book_no, Total_stock)"),
    ]
    for row in rows:
        q, ans, ex = row[:3]
        schema = row[3] if len(row) > 3 else COMPANY_SCHEMA
        add(items, q, ans, schema, f"{src}, {ex}")


def main():
    items = []
    add_silberschatz(items)
    add_elmasri(items)
    extract_learn_sql_fast(items)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(items)} items to {OUT}")


if __name__ == "__main__":
    main()
