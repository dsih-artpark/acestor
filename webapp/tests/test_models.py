from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from acestor_web.models.base import Base, TimestampMixin


def test_timestamp_mixin_defaults():
    class Dummy(Base, TimestampMixin):
        __tablename__ = "dummy_test_only"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str] = mapped_column(String(50))

    row = Dummy(id=1, name="x")
    # Defaults are DB-side; mixin only declares them
    assert hasattr(row, "created_at")
    assert hasattr(row, "updated_at")


def test_base_metadata_registered():
    assert "dummy_test_only" in Base.metadata.tables
