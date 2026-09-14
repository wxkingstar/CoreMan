"""按生效日保存模型价格，单位 USD / 百万 token。"""

from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Integer, Numeric, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base


class ModelPrice(Base):
    __tablename__ = "model_prices"
    __table_args__ = (
        CheckConstraint(
            "input_usd >= 0 AND output_usd >= 0 AND cache_read_usd >= 0 AND cache_write_usd >= 0",
            name="nonnegative",
        ),
    )
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date, primary_key=True)
    input_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    output_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    cache_read_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    cache_write_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    __mapper_args__ = {"version_id_col": version}
