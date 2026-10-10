"""Бизнес-логика сервера, не зависящая от MCP.

MCP-слой (server.py) только описывает инструменты и превращает ошибки в ответы
для агента. Всё остальное живёт здесь и тестируется как обычный Python-код.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, Field
from radar import analytics
from radar.db import VacancyRepository, VacancyRow, session_scope
from radar.etl import run_pipeline
from radar.matching import rank
from radar.models import Level
from radar.sources import SearchQuery, SourceUnavailableError, VacancySource
from radar.sources.habr import HabrCareerSource
from sqlalchemy import Engine

log = logging.getLogger(__name__)


# ---------- модели ответов: агент получает их как structured output ----------


class VacancyBrief(BaseModel):
    id: int
    title: str
    company: str
    level: str
    remote: bool
    city: str | None
    salary: str | None = Field(description="Вилка в рублях; ≈ — оценка Хабра, а не работодателя")
    skills: list[str]
    url: str


class VacancyDetails(VacancyBrief):
    specializations: list[str]
    published_at: str | None
    description: str | None


class SearchResult(BaseModel):
    total_found: int
    vacancies: list[VacancyBrief]


class MatchItem(BaseModel):
    vacancy: VacancyBrief
    score_pct: int
    matched_skills: list[str]
    missing_skills: list[str]


class SkillGap(BaseModel):
    skill: str
    missing_in_top: int = Field(
        description="Во скольких из лучших вакансий навык нужен, а в резюме его нет"
    )
    market_share_pct: float = Field(description="Доля всех вакансий в базе, где требуется навык")
    priority: float = Field(description="Чем выше, тем полезнее выучить первым")


class SkillGapReport(BaseModel):
    analyzed_vacancies: int
    already_have: list[str]
    learn_next: list[SkillGap]


class RefreshReport(BaseModel):
    source: str
    fetched: int
    inserted: int
    updated: int
    seconds: float


# ---------- форматирование ----------


def format_salary(row: VacancyRow) -> str | None:
    low, high = row.salary_from, row.salary_to
    if not low and not high:
        return None
    unit = " ₽" if row.salary_currency in (None, "RUR", "RUB") else f" {row.salary_currency}"
    if low and high and low != high:
        text = f"{low:,}–{high:,}{unit}"
    else:
        text = f"{'от ' if low else 'до '}{(low or high):,}{unit}"
    text = text.replace(",", " ")
    return f"≈ {text}" if row.salary_is_predicted else text


def to_brief(row: VacancyRow) -> VacancyBrief:
    return VacancyBrief(
        id=row.id,
        title=row.title,
        company=row.company,
        level=row.level,
        remote=row.remote,
        city=row.city,
        salary=format_salary(row),
        skills=sorted(s.name for s in row.skills),
        url=row.url,
    )


# ---------- сервис ----------


@dataclass
class RadarService:
    engine: Engine
    source_factory: Callable[[], VacancySource]
    ttl_s: float = 6 * 3600
    clock: Callable[[], float] = time.monotonic
    _refreshed_at: dict[tuple, float] = field(default_factory=dict)

    # Данные подтягиваются лениво: первый поиск сам наполняет пустую базу,
    # повторные запросы в пределах TTL идут только в БД.
    def ensure_fresh(self, query: SearchQuery) -> RefreshReport | None:
        key = (query.text.lower(), query.levels, query.remote_only)
        last = self._refreshed_at.get(key)
        if last is not None and self.clock() - last < self.ttl_s:
            return None
        return self.refresh(query)

    def refresh(self, query: SearchQuery) -> RefreshReport:
        source = self.source_factory()
        report = run_pipeline(source, query, self.engine)
        self._refreshed_at[(query.text.lower(), query.levels, query.remote_only)] = self.clock()
        return RefreshReport(**report.__dict__)

    def _safe_refresh(self, query: SearchQuery) -> None:
        try:
            self.ensure_fresh(query)
        except SourceUnavailableError as exc:
            # Источник лёг — отвечаем из того, что уже есть в базе.
            log.warning("refresh skipped: %s", exc)

    def search(
        self,
        query: str = "",
        remote_only: bool = False,
        levels: tuple[Level, ...] = (Level.INTERN, Level.JUNIOR),
        skill: str | None = None,
        limit: int = 10,
    ) -> SearchResult:
        self._safe_refresh(SearchQuery(text=query, levels=levels, remote_only=remote_only))
        with session_scope(self.engine) as session:
            rows = [
                r
                for r in VacancyRepository(session).list(
                    remote=True if remote_only else None, skill=skill, limit=1000
                )
                if r.level in {lvl.value for lvl in levels} and _matches_text(r, query)
            ]
            briefs = [to_brief(r) for r in rows[:limit]]
            return SearchResult(total_found=len(rows), vacancies=briefs)

    def get_vacancy(self, vacancy_id: int) -> VacancyDetails | None:
        with session_scope(self.engine) as session:
            row = VacancyRepository(session).get(vacancy_id)
            if row is None:
                return None
            if not row.description and row.source == "habr":
                try:
                    row.description = HabrCareerSource().fetch_description(row.to_model())
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("description fetch failed for %s: %s", vacancy_id, exc)
            return VacancyDetails(
                **to_brief(row).model_dump(),
                specializations=list(row.specializations or []),
                published_at=row.published_at.date().isoformat() if row.published_at else None,
                description=row.description,
            )

    def market_overview(self, level: Level | None = None) -> dict:
        self._safe_refresh(SearchQuery())
        return {
            "summary": analytics.summary(self.engine),
            "top_skills": analytics.top_skills(
                self.engine, limit=15, level=level.value if level else None
            ),
            "salary_by_level": analytics.salary_by_level(self.engine),
            "top_companies": analytics.top_companies(self.engine, limit=10),
        }

    def match(self, resume: str, remote_only: bool = False, top_k: int = 10) -> list[MatchItem]:
        self._safe_refresh(SearchQuery(remote_only=remote_only))
        with session_scope(self.engine) as session:
            rows = [r for r in VacancyRepository(session).all() if r.remote or not remote_only]
            by_uid = {f"{r.source}:{r.external_id}": r for r in rows}
            results = rank(resume, [r.to_model() for r in rows], top_k=top_k)
            return [
                MatchItem(
                    vacancy=to_brief(by_uid[res.vacancy.uid]),
                    score_pct=round(res.score * 100),
                    matched_skills=res.matched_skills,
                    missing_skills=res.missing_skills,
                )
                for res in results
            ]

    def skill_gap(self, resume: str, top_k: int = 15, remote_only: bool = False) -> SkillGapReport:
        """Какие навыки учить первыми: пересечение «не хватает в лучших вакансиях»
        и «востребовано на рынке в целом».

        priority = Σ score(вакансия) по лучшим вакансиям, где навыка нет
                   × (1 + доля рынка)
        Вес по score значит: навык из вакансии, которая почти подходит, важнее,
        чем навык из вакансии, до которой ещё далеко.
        """
        matches = self.match(resume, remote_only=remote_only, top_k=top_k)
        weight: dict[str, float] = defaultdict(float)
        count: dict[str, int] = defaultdict(int)
        have: set[str] = set()
        for m in matches:
            have.update(m.matched_skills)
            for skill in m.missing_skills:
                weight[skill] += m.score_pct / 100
                count[skill] += 1

        share = {
            row["skill"]: float(row["share_pct"])
            for row in analytics.top_skills(self.engine, limit=500)
        }
        gaps = [
            SkillGap(
                skill=skill,
                missing_in_top=count[skill],
                market_share_pct=share.get(skill, 0.0),
                priority=round(weight[skill] * (1 + share.get(skill, 0.0) / 100), 3),
            )
            for skill in weight
        ]
        gaps.sort(key=lambda g: (g.priority, g.missing_in_top), reverse=True)
        return SkillGapReport(
            analyzed_vacancies=len(matches), already_have=sorted(have), learn_next=gaps[:10]
        )


def _matches_text(row: VacancyRow, query: str) -> bool:
    if not query:
        return True
    haystack = " ".join([row.title, row.company, *(s.name for s in row.skills)]).lower()
    return all(word in haystack for word in query.lower().split())
