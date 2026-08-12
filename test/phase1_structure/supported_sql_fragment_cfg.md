# Supported SQL Fragment CFG for Phase 1 IR Evaluation

This document defines the grammar scope used by the Phase 1 structure-capability tests.
The grammar is intended for SQL teaching scenarios, not for full industrial SQL.

Notation:

- `[ X ]` means optional.
- `{ X }` means zero or more repetitions.
- `|` separates alternatives.
- Terminals are written in uppercase when they are SQL keywords.

## Query

```text
Query
  ::= SelectQuery
   | WithQuery
   | SetQuery

WithQuery
  ::= WITH CteDef { "," CteDef } SelectQuery
   | WITH RECURSIVE CteDef { "," CteDef } SelectQuery

CteDef
  ::= Identifier [ "(" IdentifierList ")" ] AS "(" Query ")"

SetQuery
  ::= Query SetOp Query

SetOp
  ::= UNION
   | UNION ALL
   | INTERSECT
   | INTERSECT ALL
   | EXCEPT
   | EXCEPT ALL
```

## SELECT Body

```text
SelectQuery
  ::= SELECT [ DISTINCT ] ProjectionList [ FromClause ] [ WhereClause ]
      [ GroupByClause ] [ HavingClause ] [ OrderByClause ] [ LimitClause ]

ProjectionList
  ::= ProjectionItem { "," ProjectionItem }

ProjectionItem
  ::= "*"
   | TableRef "." "*"
   | Expr [ Alias ]

Alias
  ::= AS Identifier
   | Identifier
```

## FROM and JOIN

```text
FromClause
  ::= FROM TableSource { "," TableSource }

TableSource
  ::= TableName [ Alias ]
   | "(" Query ")" Alias
   | TableSource JoinOp TableSource [ JoinCondition ]

JoinOp
  ::= JOIN
   | INNER JOIN
   | LEFT [ OUTER ] JOIN
   | RIGHT [ OUTER ] JOIN
   | FULL [ OUTER ] JOIN
   | CROSS JOIN
   | NATURAL JOIN

JoinCondition
  ::= ON Predicate
   | USING "(" IdentifierList ")"
```

## Predicates

```text
WhereClause
  ::= WHERE Predicate

HavingClause
  ::= HAVING Predicate

Predicate
  ::= Predicate AND Predicate
   | Predicate OR Predicate
   | NOT Predicate
   | "(" Predicate ")"
   | Expr CompOp Expr
   | Expr IS NULL
   | Expr IS NOT NULL
   | Expr IS DISTINCT FROM Expr
   | Expr IS NOT DISTINCT FROM Expr
   | Expr IN "(" ExprList ")"
   | Expr NOT IN "(" ExprList ")"
   | Expr IN "(" Query ")"
   | Expr NOT IN "(" Query ")"
   | Expr BETWEEN Expr AND Expr
   | Expr NOT BETWEEN Expr AND Expr
   | Expr LIKE Expr [ ESCAPE Expr ]
   | Expr NOT LIKE Expr [ ESCAPE Expr ]
   | EXISTS "(" Query ")"
   | NOT EXISTS "(" Query ")"

CompOp
  ::= "=" | "<>" | "!=" | "<" | "<=" | ">" | ">="
```

## Expressions and Aggregates

```text
Expr
  ::= ColumnRef
   | Literal
   | FunctionCall
   | AggregateCall
   | CaseExpr
   | WindowExpr
   | ScalarSubquery
   | Expr ArithmeticOp Expr
   | "-" Expr
   | "(" Expr ")"
   | CAST "(" Expr AS TypeName ")"

ArithmeticOp
  ::= "+" | "-" | "*" | "/" | "%"

AggregateCall
  ::= COUNT "(" "*" ")"
   | COUNT "(" [ DISTINCT ] Expr ")"
   | SUM "(" [ DISTINCT ] Expr ")"
   | AVG "(" [ DISTINCT ] Expr ")"
   | MIN "(" Expr ")"
   | MAX "(" Expr ")"

FunctionCall
  ::= LOWER "(" Expr ")"
   | UPPER "(" Expr ")"
   | ROUND "(" Expr [ "," Expr ] ")"
   | TRIM "(" Expr ")"
   | COALESCE "(" ExprList ")"
   | NULLIF "(" Expr "," Expr ")"
   | ABS "(" Expr ")"
```

## Grouping, Ordering, and Limits

```text
GroupByClause
  ::= GROUP BY ExprList

OrderByClause
  ::= ORDER BY OrderItem { "," OrderItem }

OrderItem
  ::= Expr [ ASC | DESC ] [ NULLS FIRST | NULLS LAST ]
   | Integer
   | AliasRef

LimitClause
  ::= LIMIT Integer
   | LIMIT Integer OFFSET Integer
   | LIMIT Integer "," Integer
   | FETCH FIRST Integer ROWS ONLY
   | TOP Integer
```

## Subqueries, CASE, and Window

```text
ScalarSubquery
  ::= "(" Query ")"

CaseExpr
  ::= CASE Expr { WHEN Expr THEN Expr } [ ELSE Expr ] END
   | CASE { WHEN Predicate THEN Expr } [ ELSE Expr ] END

WindowExpr
  ::= WindowFunction OVER "(" [ PartitionClause ] [ OrderByClause ] [ FrameClause ] ")"
   | WindowFunction OVER WindowName

WindowFunction
  ::= ROW_NUMBER "(" ")"
   | RANK "(" ")"
   | DENSE_RANK "(" ")"
   | LAG "(" Expr [ "," Expr ] [ "," Expr ] ")"
   | LEAD "(" Expr [ "," Expr ] [ "," Expr ] ")"
   | NTILE "(" Integer ")"
   | FIRST_VALUE "(" Expr ")"
   | LAST_VALUE "(" Expr ")"
   | AggregateCall

PartitionClause
  ::= PARTITION BY ExprList

FrameClause
  ::= ROWS BETWEEN FrameBound AND FrameBound
   | RANGE BETWEEN FrameBound AND FrameBound

FrameBound
  ::= UNBOUNDED PRECEDING
   | Integer PRECEDING
   | CURRENT ROW
   | Integer FOLLOWING
   | UNBOUNDED FOLLOWING
```

## Boundary Constructs

The following constructs are recorded separately because parser support,
SQLite execution support, or Phase 1 semantic support can differ by dialect:

```text
Boundary
  ::= LATERAL SubQuery
   | GROUP BY ROLLUP "(" ExprList ")"
   | GROUP BY CUBE "(" ExprList ")"
   | QUALIFY Predicate
```

## Current Known Gaps

The following constructs are represented as known gaps in the IR benchmark.
They are useful in SQL practice, but are not currently modeled as first-class
typed IR nodes:

```text
KnownGap
  ::= SELECT DISTINCT ON "(" ExprList ")"
   | GROUP BY GROUPING SETS "(" ... ")"
   | AggregateCall FILTER "(" WHERE Predicate ")"
   | RecursiveSearchCycleClause
```

The following constructs were previously retained as weak textual evidence, but
are now represented by typed IR fields in the benchmark:

```text
TypedNow
  ::= Expr IS DISTINCT FROM Expr
   | Expr IS NOT DISTINCT FROM Expr
   | Expr Quantifier "(" Query ")"
   | WindowFunction OVER WindowName
   | OrderItem COLLATE Identifier

Quantifier
  ::= ANY | ALL
```
