import logging
import time

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from models import Participant, Rank, User, engine
from riot_client import obtain_puuid, obtain_rank

log = logging.getLogger(__name__)


def find_user(game_name: str, tagline: str, db: Engine = engine) -> User | None:
    """busca un usuario por riot id sin distinguir mayusculas."""
    name, tag = game_name.strip().casefold(), tagline.strip().casefold()
    with Session(db) as session:
        for user in session.scalars(select(User)):
            if user.game_name.casefold() == name and user.tagline.casefold() == tag:
                return user
    return None


def migrate_puuid(old: str, new: str, db: Engine = engine) -> None:
    """cambia el puuid de un jugador en todas las tablas (riot lo cambia con cada api key)."""
    with Session(db) as session:
        for model in (User, Rank, Participant):
            session.execute(update(model).where(model.puuid == old).values(puuid=new))
        session.commit()
    log.info("puuid actualizado (cambió la API key): %s... → %s...", old[:8], new[:8])


def register_user(game_name: str, tagline: str, platform: str = "la1", refresh_rank: bool = True) -> str:
    """registra al jugador si no existe y actualiza su puuid y su rank; devuelve el puuid."""
    game_name, tagline = game_name.strip(), tagline.strip()

    puuid = obtain_puuid(game_name, tagline)
    user = find_user(game_name, tagline)

    if user and user.puuid != puuid:
        migrate_puuid(user.puuid, puuid)
    elif user is None:
        with Session(engine) as session:
            if session.get(User, puuid) is None:
                session.add(User(puuid=puuid, game_name=game_name, tagline=tagline, platform=platform))
                session.commit()
                log.info("Usuario %s#%s registrado", game_name, tagline)

    if refresh_rank:
        save_current_rank(puuid, platform)
    return puuid


def get_current_rank(puuid: str, queue_type: str, db: Engine = engine) -> Rank | None:
    """ultimo rank guardado del jugador en una cola."""
    with Session(db) as session:
        stmt = (
            select(Rank)
            .where(Rank.puuid == puuid, Rank.queue_type == queue_type)
            .order_by(Rank.recorded_at.desc())
            .limit(1)
        )
        return session.scalars(stmt).first()


def save_current_rank(puuid: str, platform: str = "la1") -> None:
    """consulta el rank actual del jugador en riot y lo guarda."""
    store_rank(puuid, obtain_rank(puuid, platform))


def store_rank(puuid: str, rank_entries: list[dict], db: Engine = engine) -> None:
    """guarda en la base las entradas de rank que devolvio riot."""
    now = int(time.time())

    with Session(db) as session:
        session.add_all(
            Rank(
                puuid=puuid,
                queue_type=entry["queueType"],
                tier=entry["tier"],
                rank_division=entry["rank"],
                league_points=entry["leaguePoints"],
                wins=entry["wins"],
                losses=entry["losses"],
                recorded_at=now,
            )
            for entry in rank_entries
        )
        session.commit()

    log.info("Guardadas %d entradas de rank", len(rank_entries))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    register_user("Níño", "1204")
