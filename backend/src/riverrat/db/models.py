"""ORM models (DESIGN.md §8). Schema changes go through Alembic migrations."""

from datetime import date, datetime

from geoalchemy2 import Geometry
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Reach(Base):
    """A paddleable section of river. `source` is "aw" for reaches synced from American
    Whitewater, or "manual" for hand-entered ones (which syncs never touch)."""

    __tablename__ = "reach"
    __table_args__ = (Index("ix_reach_geom", "geom", postgresql_using="gist"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    aw_id: Mapped[int | None] = mapped_column(unique=True)
    source: Mapped[str] = mapped_column(String(16), default="aw")
    river: Mapped[str]
    section: Mapped[str]
    altname: Mapped[str] = mapped_column(default="")
    difficulty_raw: Mapped[str | None] = mapped_column(String(32))
    class_low: Mapped[str | None] = mapped_column(String(8))
    class_high: Mapped[str | None] = mapped_column(String(8))
    class_standout: Mapped[str | None] = mapped_column(String(8))
    states: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    length_mi: Mapped[float | None]
    gradient_fpm: Mapped[float | None]
    geom = mapped_column(Geometry(srid=4326, spatial_index=False), nullable=True)
    next_release: Mapped[date | None]
    aw_updated_at: Mapped[datetime | None]
    # Last time the reach appeared in AW's index / had its detail page parsed.
    listed_at: Mapped[datetime | None]
    scraped_at: Mapped[datetime | None]

    gauges: Mapped[list["ReachGauge"]] = relationship(
        back_populates="reach", cascade="all, delete-orphan"
    )


class Gauge(Base):
    """A flow gauge. `source` is the agency/kind as AW reports it: "USGS", "NOAA" (NWS
    location ID), "CALCULATED" (AW virtual gauge), ..."""

    __tablename__ = "gauge"
    __table_args__ = (
        UniqueConstraint("source", "source_id"),
        Index("ix_gauge_geom", "geom", postgresql_using="gist"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str]

    # Site metadata from the gauge's agency (USGS for now).
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=True)
    agency_name: Mapped[str | None]
    drainage_area_sqmi: Mapped[float | None]
    state_code: Mapped[str | None] = mapped_column(String(2))
    huc: Mapped[str | None] = mapped_column(String(16))
    # Period of record of the 15-minute discharge (cfs) and stage (ft) series.
    cfs_begin: Mapped[datetime | None]
    cfs_end: Mapped[datetime | None]
    ft_begin: Mapped[datetime | None]
    ft_end: Mapped[datetime | None]
    metadata_synced_at: Mapped[datetime | None]
    # NOAA links: NWS location ID (forecast points only) and the NWM reach (NHDPlus COMID).
    nws_lid: Mapped[str | None] = mapped_column(String(8))
    nwm_reach_id: Mapped[int | None] = mapped_column(BigInteger)
    has_rfc_forecast: Mapped[bool] = mapped_column(default=False, server_default="false")
    noaa_linked_at: Mapped[datetime | None]


class ReachGauge(Base):
    """A gauge linked to a reach, with AW's runnable range in that gauge's units.
    `range_min`/`range_max` are None when AW gives no bound."""

    __tablename__ = "reach_gauge"
    __table_args__ = (UniqueConstraint("reach_id", "gauge_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reach_id: Mapped[int] = mapped_column(ForeignKey("reach.id", ondelete="CASCADE"))
    gauge_id: Mapped[int] = mapped_column(ForeignKey("gauge.id"))
    is_primary: Mapped[bool]
    metric: Mapped[str | None] = mapped_column(String(16))
    range_min: Mapped[float | None]
    range_max: Mapped[float | None]
    aw_status: Mapped[str | None] = mapped_column(String(32))

    reach: Mapped[Reach] = relationship(back_populates="gauges")
    gauge: Mapped[Gauge] = relationship()


class Observation(Base):
    """Recent gauge readings (the server keeps ~60 days; full history lives in Parquet).
    `param` is "cfs" or "ft"; `approved` is False while the agency's data is provisional."""

    __tablename__ = "observation"

    gauge_id: Mapped[int] = mapped_column(
        ForeignKey("gauge.id", ondelete="CASCADE"), primary_key=True
    )
    param: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    value: Mapped[float]
    approved: Mapped[bool]
    qualifier: Mapped[str | None] = mapped_column(String(32))


class NoaaForecast(Base):
    """Archived NOAA flow forecasts, kept forever for benchmarking (DESIGN.md §6.7).
    `source` is "rfc" (official River Forecast Center) or "nwm_short" (National Water
    Model short range). `issued_at` is the RFC issue time or the NWM reference time."""

    __tablename__ = "noaa_forecast"

    gauge_id: Mapped[int] = mapped_column(
        ForeignKey("gauge.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    issued_at: Mapped[datetime] = mapped_column(primary_key=True)
    valid_time: Mapped[datetime] = mapped_column(primary_key=True)
    flow_cfs: Mapped[float | None]
    stage_ft: Mapped[float | None]
