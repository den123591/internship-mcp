# internship-mcp — инструкции для AI-агентов

- Установка: `pip install -e ".[dev]"`; тесты: `pytest`; линтер: `ruff check src tests && ruff format --check src tests`
- Smoke через stdio: `python scripts/smoke_stdio.py`
- SDK — `mcp` 2.x: `from mcp.server.mcpserver import MCPServer`. Не писать код под 1.x (`FastMCP`).
- Бизнес-логика только в `service.py`; `server.py` — описания инструментов и преобразование ошибок.
- Ожидаемые ошибки — `ToolError` с подсказкой агенту, что делать дальше.
- Сбор/очистку/аналитику не дублировать — использовать пакет `radar` (internship-radar).
- Тексты вакансий в промптах оборачивать в теги и помечать как недоверенные данные.
- Async-тесты — через плагин anyio (`pytestmark = pytest.mark.anyio`), не pytest-asyncio.
