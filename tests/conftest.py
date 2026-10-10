import pytest
from mcp import Client
from radar.db import make_engine
from radar.sources import SampleSource

from internship_mcp.server import create_server
from internship_mcp.service import RadarService

RESUME = (
    "Студент 4 курса. Python, SQL, PostgreSQL, Git, Linux, Docker. "
    "Писал REST API на FastAPI, анализ данных в Pandas. ООП, алгоритмы."
)


class CountingSource(SampleSource):
    """Офлайн-снимок вакансий, который считает обращения — для проверки кеша."""

    calls = 0

    def fetch(self, query):
        type(self).calls += 1
        return super().fetch(query)


@pytest.fixture
def service():
    CountingSource.calls = 0
    engine = make_engine("sqlite://")
    now = {"t": 0.0}
    svc = RadarService(engine, CountingSource, ttl_s=60, clock=lambda: now["t"])
    svc._now = now  # тесты двигают время вручную
    yield svc
    engine.dispose()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(service):
    async with Client(create_server(service), raise_exceptions=False) as c:
        yield c
