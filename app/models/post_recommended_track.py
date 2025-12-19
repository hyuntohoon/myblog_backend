from sqlalchemy import Table, Column, ForeignKey, SmallInteger, Text, ForeignKeyConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from app.db.base import Base

post_recommended_tracks = Table(
    "post_recommended_tracks",
    Base.metadata,
    Column("post_id", PGUUID(as_uuid=True), nullable=False),
    Column("album_id", PGUUID(as_uuid=True), nullable=False),
    Column("track_id", PGUUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True),
    Column("position", SmallInteger, nullable=True),
    Column("note", Text, nullable=True),
    ForeignKeyConstraint(
        ["post_id", "album_id"],
        ["post_albums.post_id", "post_albums.album_id"],
        ondelete="CASCADE",
    ),
)