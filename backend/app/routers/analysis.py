"""提供 YAML 分析技能清单。"""
from fastapi import APIRouter

from app.services import analysis_skill_loader

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


@router.get("/skills")
def list_analysis_skills():
    ids = analysis_skill_loader.list_skill_ids()
    items = []
    for sid in ids:
        d = analysis_skill_loader.load_skill(sid)
        items.append(
            {
                "id": sid,
                "display_name": d.get("display_name") or sid,
                "description": (d.get("description") or "")[:500],
            }
        )
    return {"skills": items}
