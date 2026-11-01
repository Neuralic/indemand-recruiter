"""Role config loader: roles/<id>.yaml -> validated dataclasses. Placeholder blocks are tracked so they can never be silently
mistaken for posting-sourced content."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from guardrails import spoken_text_problems

ROLES_DIR = Path(__file__).resolve().parents[1] / "roles"


@dataclass
class Competency:
    id: str
    name: str
    primary_question: str
    probe_if_vague: str
    strong_signals: list[str]
    weak_signals: list[str]
    weight: float
    placeholder: bool = False
    short_question: str = ""  # spoken when the question must be repeated mid-interview (never the full read)


@dataclass
class Knockout:
    id: str
    question: str
    fail_if: str
    required: bool = True
    placeholder: bool = False
    short_question: str = ""


@dataclass
class RoleConfig:
    id: str
    title: str
    location: str
    country: str
    ontario: bool
    facts: dict[str, str]
    script: dict[str, str]
    competencies: list[Competency]
    knockouts: list[Knockout]
    domain_terms: list[str]
    placeholders: list[str] = field(default_factory=list)  # human-readable list of every block still marked PLACEHOLDER

    def known_fact(self, key: str) -> str | None:
        v = (self.facts.get(key) or "").strip()
        return v if v and "PLACEHOLDER" not in v.upper() else None


def load_role(role_id: str, roles_dir: Path = ROLES_DIR) -> RoleConfig:
    raw = yaml.safe_load((roles_dir / f"{role_id}.yaml").read_text(encoding="utf-8"))
    comps = [Competency(id=c["id"], name=c["name"], primary_question=c["primary_question"], probe_if_vague=c["probe_if_vague"],
                        strong_signals=c.get("strong_signals", []), weak_signals=c.get("weak_signals", []),
                        weight=float(c.get("weight", 0)), placeholder=bool(c.get("placeholder")),
                        short_question=c.get("short_question", ""))
             for c in raw["competencies"]]
    kos = [Knockout(id=k["id"], question=k["question"], fail_if=k.get("fail_if", ""), required=bool(k.get("required", True)),
                    placeholder=bool(k.get("placeholder")), short_question=k.get("short_question", "")) for k in raw["knockouts"]]
    cfg = RoleConfig(id=str(raw["id"]), title=raw["title"], location=raw["location"], country=raw["country"],
                     ontario=bool(raw.get("ontario")), facts=dict(raw.get("facts_for_candidate_questions") or {}),
                     script=dict(raw["script"]), competencies=comps, knockouts=kos, domain_terms=list(raw.get("domain_terms", [])))
    for c in comps:
        if c.placeholder:
            cfg.placeholders.append(f"competency:{c.id}")
    for k in kos:
        if k.placeholder:
            cfg.placeholders.append(f"knockout:{k.id}")
    for flag in ("summary_placeholder", "selling_points_placeholder"):
        if raw.get(flag):
            cfg.placeholders.append(flag.replace("_placeholder", ""))
    if raw["script"].get("placeholder"):
        cfg.placeholders.append("script")
    for key, v in cfg.facts.items():
        if not cfg.known_fact(key):
            cfg.placeholders.append(f"fact:{key} (not stated in the posting -> deferred to Stefanie)")
    _validate(cfg)
    return cfg


def _validate(cfg: RoleConfig) -> None:
    """Every line the agent may speak must pass the §14 pronunciation/markup rules and the §7 protected-ground rule."""
    spoken = {f"script.{k}": v for k, v in cfg.script.items() if isinstance(v, str)}
    spoken.update({f"competency.{c.id}.primary": c.primary_question for c in cfg.competencies})
    spoken.update({f"competency.{c.id}.probe": c.probe_if_vague for c in cfg.competencies})
    spoken.update({f"knockout.{k.id}": k.question for k in cfg.knockouts})
    spoken.update({f"competency.{c.id}.short": c.short_question for c in cfg.competencies if c.short_question})
    spoken.update({f"knockout.{k.id}.short": k.short_question for k in cfg.knockouts if k.short_question})
    spoken.update({f"fact.{k}": v for k, v in cfg.facts.items() if cfg.known_fact(k)})
    bad = {k: p for k, v in spoken.items() if (p := spoken_text_problems(v))}
    if bad:
        raise ValueError(f"role {cfg.id}: spoken text violates guardrails: {bad}")
    if abs(sum(c.weight for c in cfg.competencies) - 1.0) > 0.01:
        raise ValueError(f"role {cfg.id}: competency weights must sum to 1.0")
    if len({c.id for c in cfg.competencies}) != len(cfg.competencies):
        raise ValueError(f"role {cfg.id}: duplicate competency ids")
