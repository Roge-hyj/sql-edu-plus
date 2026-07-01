from pydantic import BaseModel, Field
from typing import List, Annotated, Literal

class SQLDiagnosisSchema(BaseModel):
    """
    具体的 SQL 诊断详情。
    在大数据分析中，这对应于一条诊断记录。
    """
    
    error_type: Annotated[
        Literal["语法错误", "逻辑错误", "性能问题", "业务理解偏差", "无"], 
        Field(..., description="错误的分类，必须从中选一")
    ]
    
    is_correct: Annotated[bool, Field(..., description="此项是否完全符合题目要求")]
    
    knowledge_point: Annotated[str, Field(..., description="该错误涉及的知识点，如：'GROUP BY 子句', '聚合函数用法'")]
    
    hint: Annotated[str, Field(..., description="苏格拉底式引导语。绝对不能给正确 SQL，必须是反问句。")]
    
    explanation: Annotated[str, Field(..., description="用简练的语言解释为什么这样写是不对的，但不给答案。")]

class SQLCheckResultSchema(BaseModel):
    """
    AI 导师返回的完整响应报告。
    """
    # 这是一个嵌套结构：一份报告包含多个诊断项
    diagnoses: List[SQLDiagnosisSchema]
    
    overall_comment: Annotated[str, Field(..., description="对学生本次尝试的整体评价和鼓励语")]