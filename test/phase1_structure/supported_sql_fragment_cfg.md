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
      [ RecursiveDecoration ]

RecursiveDecoration
  ::= SEARCH ( DEPTH | BREADTH ) FIRST BY ExprList SET Identifier
   | CYCLE ExprList SET Identifier [ TO Expr DEFAULT Expr ]
      USING Identifier

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
  ::= SELECT [ DistinctClause ] ProjectionList [ FromClause ] [ WhereClause ]
      [ GroupByClause ] [ HavingClause ] [ QualifyClause ] [ OrderByClause ]
      [ LimitClause ]

DistinctClause
  ::= DISTINCT
   | DISTINCT ON "(" ExprList ")"

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
   | LATERAL "(" Query ")" Alias
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
  ::= AggregateCore [ AggregateFilterClause ]

AggregateCore
  ::= COUNT "(" "*" ")"
   | COUNT "(" [ DISTINCT ] Expr ")"
   | SUM "(" [ DISTINCT ] Expr ")"
   | AVG "(" [ DISTINCT ] Expr ")"
   | MIN "(" Expr ")"
   | MAX "(" Expr ")"

AggregateFilterClause
  ::= FILTER "(" WHERE Predicate ")"

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
  ::= GROUP BY GroupingElement { "," GroupingElement }

GroupingElement
  ::= Expr
   | GROUPING SETS "(" GroupingSet { "," GroupingSet } ")"
   | ROLLUP "(" ExprList ")"
   | CUBE "(" ExprList ")"

GroupingSet
  ::= "(" [ ExprList ] ")"
   | Expr

QualifyClause
  ::= QUALIFY Predicate

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

The following constructs have first-class structure recognition and AST-diff
support, but remain explicit backend execution boundaries in the current
SQLite-based counterexample path:

```text
ExecutionBoundary
  ::= LATERAL SubQuery
   | GROUP BY GROUPING SETS "(" GroupingSet { "," GroupingSet } ")"
   | GROUP BY ROLLUP "(" ExprList ")"
   | GROUP BY CUBE "(" ExprList ")"
   | Query INTERSECT ALL Query
   | Query EXCEPT ALL Query
```

## Current Typed Coverage

The current IR benchmark has `77/77` typed structures and no known
typed-structure gaps. The independent AST Diff benchmark supports `53/53`
targeted pairs, and the linked IR-to-AST benchmark supports `77/77` pairs. The
following constructs were previously gaps or dialect boundaries and are now
represented by the formal productions above, first-class typed IR fields, and
dedicated AST differences:

```text
TypedNow
  ::= SELECT DISTINCT ON "(" ExprList ")"
   | GROUP BY GROUPING SETS "(" ... ")"
   | AggregateCall FILTER "(" WHERE Predicate ")"
   | RecursiveSearchCycleClause
   | QUALIFY Predicate
   | LATERAL SubQuery
   | GROUP BY ROLLUP "(" ExprList ")"
   | GROUP BY CUBE "(" ExprList ")"
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

`GROUPING SETS`, `LATERAL`, `ROLLUP`, `CUBE`, `INTERSECT ALL`, and `EXCEPT ALL`
are structurally typed and AST-diff supported, while being marked separately as
SQLite execution boundaries because that backend cannot preserve their
semantics. Execution status does not reduce typed IR or AST-diff coverage. A
single recursive CTE containing both `SEARCH` and `CYCLE` remains dependent on
parser support; standalone `SEARCH` and `CYCLE` decorations are typed.
