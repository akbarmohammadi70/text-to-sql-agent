
import os
from dotenv import load_dotenv
from langchain_community.utilities import SQLDatabase
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import (
    StrOutputParser,
    JsonOutputParser,
)
from sqlalchemy.exc import SQLAlchemyError

# ---------- 1. Environment and initialization ----------

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openai/gpt-4o-mini",
)

if not OPENROUTER_API_KEY:
    raise EnvironmentError(
        "OPENROUTER_API_KEY is missing. "
        "Please configure it in the .env file."
    )

# Connect to SQLite database
db = SQLDatabase.from_uri("sqlite:///bank_database.db")

# Initialize LLM through OpenRouter
llm = ChatOpenAI(
    model=OPENROUTER_MODEL,
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    temperature=0,
    max_tokens=2048,
)

# Retrieve database schema
schema = db.get_table_info()


# ---------- 2. LangChain prompts and chains ----------

generate_chain = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are a SQLite expert. Database schema:\n{schema}\n\n"
        "Rules:\n"
        "- Return only one SQLite SELECT query.\n"
        "- Do not include explanations or Markdown.\n"
        '- Always quote the table name "Transaction".\n'
        "- If the question can be answered from the schema alone, "
        "return exactly: NO_SQL",
    ),
    ("human", "{question}"),
]) | llm | StrOutputParser()


schema_answer_chain = ChatPromptTemplate.from_messages([
    (
        "system",
        "Answer the user's question using only this database schema:\n"
        "{schema}\n"
        "Answer in the same language as the question.",
    ),
    ("human", "{question}"),
]) | llm | StrOutputParser()


fix_syntax_chain = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are a SQLite expert. Database schema:\n{schema}\n\n"
        "Fix the failed SQL query.\n"
        "Return only one corrected SQLite SELECT query. "
        "Do not include explanations or Markdown.",
    ),
    (
        "human",
        "Question: {question}\n"
        "Query: {sql}\n"
        "Error: {error}",
    ),
]) | llm | StrOutputParser()


semantic_chain = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are a SQLite expert. Database schema:\n{schema}\n\n"
        "Evaluate whether the SQL query logically answers the question.\n"
        "Check JOINs, filters, aggregations, and missing conditions.\n\n"
        "Return only a valid JSON object with these fields:\n"
        '{{"has_problem": false, "reason": "short explanation", '
        '"corrected_sql": null}}\n\n'
        "If a problem exists, set has_problem to true and provide "
        "a corrected SELECT query in corrected_sql.\n"
        "Otherwise, set corrected_sql to null.",
    ),
    (
        "human",
        "Question: {question}\n"
        "Query: {sql}\n"
        "Result: {result}",
    ),
]) | llm | JsonOutputParser()


# ---------- 3. Helper functions ----------

def clean_sql(text):
    """Remove Markdown SQL code fences and whitespace."""
    return (
        text.replace("```sql", "")
        .replace("```", "")
        .strip()
    )


def is_read_only_query(query):
    """Basic validation for a single SELECT query."""
    normalized = query.strip().lower()

    if not normalized:
        return False

    # Reject multiple SQL statements
    if ";" in normalized.rstrip(";"):
        return False

    # Allow only SELECT statements
    if not normalized.startswith("select "):
        return False

    forbidden_keywords = [
        "insert ",
        "update ",
        "delete ",
        "drop ",
        "alter ",
        "create ",
        "attach ",
        "detach ",
        "pragma ",
        "replace ",
    ]

    return not any(
        keyword in normalized
        for keyword in forbidden_keywords
    )


def execute_sql_safely(query):
    """Validate and execute a read-only SQL query."""

    if not is_read_only_query(query):
        return {
            "status": "error",
            "message": "Only single SELECT queries are allowed.",
        }

    try:
        return {
            "status": "success",
            "data": db.run(query),
        }

    except SQLAlchemyError as error:
        return {
            "status": "error",
            "message": str(error),
        }


def fix_sql_syntax(question, sql, error):
    """Fix SQL syntax errors using the LLM."""

    output = fix_syntax_chain.invoke({
        "schema": schema,
        "question": question,
        "sql": sql,
        "error": error,
    })

    return clean_sql(output)


def reflect_and_fix_semantic_issue(question, sql, result):
    """Detect and correct logical SQL issues."""

    answer = semantic_chain.invoke({
        "schema": schema,
        "question": question,
        "sql": sql,
        "result": result,
    })

    if not isinstance(answer, dict):
        raise ValueError(
            "Invalid semantic reflection response."
        )

    has_problem = answer.get("has_problem")
    corrected_sql = answer.get("corrected_sql")

    if has_problem and corrected_sql:
        print("   Semantic issue:", answer.get("reason"))
        return clean_sql(corrected_sql)

    return None


# ---------- 4. Main Text-to-SQL agent ----------

def text_to_sql_agent(question, max_syntactic_attempts=3):
    """Generate, validate, execute, and evaluate SQL queries."""

    if max_syntactic_attempts < 1:
        raise ValueError(
            "max_syntactic_attempts must be at least 1."
        )

    print(f"\nQuestion: {question}")

    # Step 1: Generate SQL
    current_sql = clean_sql(
        generate_chain.invoke({
            "schema": schema,
            "question": question,
        })
    )

    # Handle schema-only questions
    if current_sql == "NO_SQL":
        answer = schema_answer_chain.invoke({
            "schema": schema,
            "question": question,
        })
        print(answer)
        return answer

    # Step 2: Execute SQL and retry syntax errors
    result = None

    for attempt in range(1, max_syntactic_attempts + 1):

        print(f"[Syntax attempt {attempt}] SQL: {current_sql}")

        result = execute_sql_safely(current_sql)

        if result["status"] == "success":
            break

        print("   SQL error:", result["message"])

        if attempt == max_syntactic_attempts:
            return (
                f"Failed to generate a valid SQL query after "
                f"{max_syntactic_attempts} attempts."
            )

        current_sql = fix_sql_syntax(
            question,
            current_sql,
            result["message"],
        )

    # Step 3: Evaluate semantic correctness
    corrected_sql = reflect_and_fix_semantic_issue(
        question,
        current_sql,
        result["data"],
    )

    # Execute corrected SQL if necessary
    if corrected_sql is not None:

        print("[Semantic correction] New SQL:", corrected_sql)

        result = execute_sql_safely(corrected_sql)

        if result["status"] == "error":
            return (
                "Semantic correction produced an invalid query: "
                f"{result['message']}"
            )

    # Step 4: Return final result
    print("Output:", result["data"])

    return result["data"]


# ---------- 5. Evaluation questions ----------

questions = [
    "چه داده‌هایی داری؟",

    "نام و نام خانوادگی تمام مشتریانی که بعد از تاریخ "
    "'2021-01-01' ثبت‌نام کرده‌اند را نشان بده.",

    "لیست تمام حساب‌های فعال (Active) بانکی به همراه "
    "موجودی آن‌ها را بیاور.",

    "فهرست مشتریانی که حساب آن‌ها غیرفعال است را گزارش کنید.",

    "فهرست مشتریانی که بیشترین مبلغ تراکنش "
    "(بزرگ‌ترین تک تراکنش) را دارند گزارش کنید.",

    "مجموع مبالغ واریز شده (Deposit) برای حساب با شماره حساب "
    "'ACC200001' چقدر است؟",

    "فهرست حساب‌هایی که فقط نوع تراکنش آن‌ها برداشت "
    "(Withdrawal) می‌باشد را گزارش کنید.",

    "فهرست مشتریانی را به ترتیب کل حجم مالی تراکنش‌ها "
    "(مجموع مبالغ واریز، برداشت، جریمه و...) گزارش کنید.",

    "لیست مشتریانی را بده که در جدول انتقال وجه (Transfer)، "
    "هم به عنوان مبدأ و هم به عنوان مقصد حضور داشته‌اند "
    "و مبالغ تراکنش‌های آن‌ها را مشخص کن.",
]


# ---------- 6. Application entry point ----------

if __name__ == "__main__":

    print(f"LLM Provider: OpenRouter")
    print(f"LLM Model: {OPENROUTER_MODEL}")

    for question in questions:
        try:
            text_to_sql_agent(question)

        except Exception as error:
            print(f"Agent execution failed: {error}")
