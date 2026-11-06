"""技能：读取技能库、解析一次研究或规划选中的技能。"""
from __future__ import annotations

from dra import skills
from dra.api.services.errors import InvalidRequest, Unavailable


def skill_repository():
    from dra.paths import DATA_DIR
    return skills.SkillRepository(DATA_DIR)


def _skill_operation(fn, *args):
    try:
        return fn(*args)
    except skills.SkillError as exc:
        raise InvalidRequest(str(exc)) from None
    except OSError:
        raise Unavailable("技能文件读写失败，请检查数据目录；未确认保存成功") from None


def skill_bundle(identity):
    """按准备好的技能选择 ID 取出技能包；没选技能时返回空包。"""
    repo = skill_repository()
    return _skill_operation(repo.resolve, identity) if identity else {}


def list_skills():
    return _skill_operation(skill_repository().list)


def set_enabled(enabled: list[str]):
    _skill_operation(skill_repository().set_enabled, enabled)


def prepare(selected: list[str]) -> dict:
    bundle = _skill_operation(skill_repository().prepare, selected)
    return {"skill_selection": bundle["id"], "selected": bundle["selected"]}


def save(name: str, text: str, references: dict[str, str]):
    return _skill_operation(skill_repository().save, name, text, references)
