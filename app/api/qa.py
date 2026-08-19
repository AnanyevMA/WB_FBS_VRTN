"""
QA Router — Эндпоинты запуска автоматического тестировщика
"""
from fastapi import APIRouter
from app.agents.qa_test_agent import run_system_regression_tests

router = APIRouter(prefix="/qa", tags=["qa"])


@router.post("/run-tests")
async def run_qa_tests():
    """Запустить сквозной регрессионный тест системы."""
    res = run_system_regression_tests()
    return res
