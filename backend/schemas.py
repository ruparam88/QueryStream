"""
Pydantic schemas for QueryStream structured LLM outputs.
These models act as both the runtime validation layer and
the JSON schema passed to the Gemini structured-output API.
"""
from pydantic import BaseModel, Field


class SQLQueryResponse(BaseModel):
    """Structured output schema for SQL (PostgreSQL / MySQL) query generation."""

    thought_process: str = Field(
        description=(
            "Step-by-step reasoning: identify the relevant tables, describe any "
            "joins required, clarify filter conditions, and explain projection choices. "
            "Think aloud before writing the query."
        )
    )
    query: str = Field(
        description=(
            "The complete, executable SQL SELECT statement. "
            "Must be valid SQL for the target dialect. "
            "Do NOT include markdown fences, comments, or trailing semicolons."
        )
    )
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Estimated probability (0.0–1.0) that this query correctly answers "
            "the user intent given the available schema information."
        ),
    )
    requires_destructive_operation: bool = Field(
        description=(
            "True if the query contains or requires any state-changing operation "
            "(INSERT, UPDATE, DELETE, DROP, TRUNCATE, ALTER, CREATE). "
            "False for pure SELECT / read-only queries."
        )
    )


class MongoQueryResponse(BaseModel):
    """Structured output schema for MongoDB query generation."""

    thought_process: str = Field(
        description=(
            "Step-by-step reasoning: identify the target collection, describe "
            "the filter logic, and explain any projections or sort orders needed."
        )
    )
    collection: str = Field(
        description="The exact name of the MongoDB collection to query."
    )
    filter: dict = Field(
        description=(
            "A valid MongoDB filter document (JSON object) suitable for "
            "db.<collection>.find(<filter>). Use {} for no filter."
        )
    )
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Estimated probability (0.0–1.0) that this filter correctly answers "
            "the user intent."
        ),
    )
    requires_destructive_operation: bool = Field(
        description=(
            "True if the intent requires an insert, update, delete, or drop. "
            "False for read-only find queries."
        )
    )
    