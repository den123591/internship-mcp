from types import SimpleNamespace

from radar.models import Level
from radar.sources import SearchQuery, SourceUnavailableError

from internship_mcp.service import format_salary

from .conftest import RESUME, CountingSource


def test_first_search_populates_empty_db(service):
    result = service.search()
    assert result.total_found > 0
    assert CountingSource.calls == 1


def test_refresh_is_cached_within_ttl(service):
    service.search("python")
    service.search("python")
    assert CountingSource.calls == 1

    service._now["t"] += 61  # TTL истёк
    service.search("python")
    assert CountingSource.calls == 2


def test_different_queries_are_cached_separately(service):
    service.search("python")
    service.search("java")
    assert CountingSource.calls == 2


def test_search_filters(service):
    remote = service.search(remote_only=True, limit=50)
    assert remote.vacancies and all(v.remote for v in remote.vacancies)

    interns = service.search(levels=(Level.INTERN,), limit=50)
    assert {v.level for v in interns.vacancies} == {"intern"}

    sql = service.search(skill="SQL", limit=50)
    assert all("SQL" in v.skills for v in sql.vacancies)


def test_search_survives_source_outage(service):
    service.search()  # база наполнена

    def broken():
        raise SourceUnavailableError("down")

    service.source_factory = lambda: SimpleNamespace(name="broken", fetch=lambda q: broken())
    service._now["t"] += 1000
    assert service.search().total_found > 0  # отвечаем из базы


def test_get_vacancy(service):
    first = service.search(limit=1).vacancies[0]
    details = service.get_vacancy(first.id)
    assert details.title == first.title
    assert service.get_vacancy(10**9) is None


def test_match_and_skill_gap(service):
    matches = service.match(RESUME, top_k=5)
    assert len(matches) == 5
    assert matches == sorted(matches, key=lambda m: m.score_pct, reverse=True)

    gap = service.skill_gap(RESUME)
    assert gap.analyzed_vacancies > 0
    assert "Python" in gap.already_have
    learn = [g.skill for g in gap.learn_next]
    assert learn and not set(learn) & set(gap.already_have)
    priorities = [g.priority for g in gap.learn_next]
    assert priorities == sorted(priorities, reverse=True)


def test_market_overview(service):
    data = service.market_overview()
    assert data["summary"]["total"] > 0
    assert data["top_skills"][0]["rank"] == 1


def test_refresh_report(service):
    report = service.refresh(SearchQuery())
    assert report.source == "sample" and report.fetched > 0


def _row(**kw):
    base = dict(salary_from=None, salary_to=None, salary_currency="RUR", salary_is_predicted=False)
    return SimpleNamespace(**{**base, **kw})


def test_format_salary():
    assert format_salary(_row()) is None
    assert format_salary(_row(salary_from=50000, salary_to=80000)) == "50 000–80 000 ₽"
    assert format_salary(_row(salary_from=50000)) == "от 50 000 ₽"
    assert format_salary(_row(salary_to=1000, salary_currency="USD")) == "до 1 000 USD"
    assert format_salary(_row(salary_from=1, salary_to=2, salary_is_predicted=True)).startswith("≈")
