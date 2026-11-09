from pathlib import Path

from sqlalchemy import BigInteger, Boolean, Column, Float, ForeignKey, Index, Integer, String, create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool

# Rutas absolutas: funcionan igual si ejecutas desde lol-coach/ o desde app/.
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "lol.db"

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    puuid = Column(String, primary_key=True)
    game_name = Column(String)
    tagline = Column(String)
    platform = Column(String)


class Rank(Base):
    __tablename__ = "ranks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    puuid = Column(String, ForeignKey("users.puuid"), index=True)
    queue_type = Column(String)
    tier = Column(String)
    rank_division = Column(String)
    league_points = Column(Integer)
    wins = Column(Integer)
    losses = Column(Integer)
    recorded_at = Column(Integer)


class Match(Base):
    __tablename__ = "matches"

    match_id = Column(String, primary_key=True)
    game_version = Column(String)
    game_duration = Column(Integer)  # segundos
    game_creation = Column(BigInteger)  # epoch en milisegundos
    game_mode = Column(String)
    end_game_result = Column(String)


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        Index("ux_participants_match_puuid", "match_id", "puuid", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(String, ForeignKey("matches.match_id"), index=True)
    puuid = Column(String, index=True)
    champion_name = Column(String)
    team_position = Column(String)
    team_id = Column(Integer)
    win = Column(Boolean)
    kills = Column(Integer)
    deaths = Column(Integer)
    assists = Column(Integer)
    gold_earned = Column(Integer)
    damage_dealt = Column(Integer)
    damage_taken = Column(Integer)
    heals_on_teammates = Column(Integer)
    vision_score = Column(Integer)
    cs = Column(Integer)
    kill_participation = Column(Float)  # 0.0 - 1.0


DATA_DIR.mkdir(exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"timeout": 15},
    poolclass=NullPool,
)

Base.metadata.create_all(engine)
# create_all no agrega índices a tablas que ya existían, así que los creamos aparte.
for _table in Base.metadata.sorted_tables:
    for _index in _table.indexes:
        _index.create(engine, checkfirst=True)
