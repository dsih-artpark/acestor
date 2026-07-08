import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from acestor_web.audit import write_audit
from acestor_web.db import get_db
from acestor_web.deps import get_current_user
from acestor_web.models import ConfigPreset, User
from acestor_web.schemas import (
    ConfigPresetCreate,
    ConfigPresetOut,
    ConfigPresetUpdate,
)

router = APIRouter(prefix="/api/presets", tags=["presets"])


def _snapshot(p: ConfigPreset) -> dict:
    return {
        "name": p.name,
        "description": p.description,
        "yaml_text": p.yaml_text,
        "archived": p.archived,
    }


@router.get("", response_model=list[ConfigPresetOut])
def list_presets(
    include_archived: bool = Query(False),
    session: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[ConfigPreset]:
    q = session.query(ConfigPreset)
    if not include_archived:
        q = q.filter(ConfigPreset.archived.is_(False))
    return q.order_by(ConfigPreset.name).all()


@router.post("", response_model=ConfigPresetOut, status_code=status.HTTP_201_CREATED)
def create_preset(
    payload: ConfigPresetCreate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConfigPreset:
    existing = session.query(ConfigPreset).filter_by(name=payload.name).one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="preset name already exists")
    preset = ConfigPreset(
        id=uuid.uuid4(),
        name=payload.name,
        description=payload.description,
        yaml_text=payload.yaml_text,
        created_by=user.id,
    )
    session.add(preset)
    session.flush()
    write_audit(
        session,
        user_id=user.id,
        action="preset.create",
        target_type="preset",
        target_id=preset.id,
        after=_snapshot(preset),
    )
    session.commit()
    session.refresh(preset)
    return preset


@router.get("/{preset_id}", response_model=ConfigPresetOut)
def get_preset(
    preset_id: uuid.UUID,
    session: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ConfigPreset:
    preset = session.get(ConfigPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="preset not found")
    return preset


@router.patch("/{preset_id}", response_model=ConfigPresetOut)
def update_preset(
    preset_id: uuid.UUID,
    payload: ConfigPresetUpdate,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConfigPreset:
    preset = session.get(ConfigPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="preset not found")
    before = _snapshot(preset)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(preset, field, value)
    session.flush()
    write_audit(
        session,
        user_id=user.id,
        action="preset.update",
        target_type="preset",
        target_id=preset.id,
        before=before,
        after=_snapshot(preset),
    )
    session.commit()
    session.refresh(preset)
    return preset


@router.delete("/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_preset(
    preset_id: uuid.UUID,
    session: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    preset = session.get(ConfigPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="preset not found")
    if preset.archived:
        return
    before = _snapshot(preset)
    preset.archived = True
    session.flush()
    write_audit(
        session,
        user_id=user.id,
        action="preset.archive",
        target_type="preset",
        target_id=preset.id,
        before=before,
        after=_snapshot(preset),
    )
    session.commit()
