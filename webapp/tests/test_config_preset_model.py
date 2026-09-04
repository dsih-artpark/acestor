import uuid

from acestor_web.models import AuthProvider, ConfigPreset, User


def test_create_preset(db_session):
    user = User(
        id=uuid.uuid4(),
        email="a@b.c",
        auth_provider=AuthProvider.local,
        password_hash="stub",
    )
    db_session.add(user)
    db_session.flush()

    preset = ConfigPreset(
        id=uuid.uuid4(),
        name="ap-district-ensemble",
        description="AP districts, ensemble output",
        yaml_text="pipeline: acestor\n",
        created_by=user.id,
    )
    db_session.add(preset)
    db_session.flush()

    got = db_session.query(ConfigPreset).filter_by(name="ap-district-ensemble").one()
    assert got.yaml_text.startswith("pipeline:")
    assert got.archived is False
