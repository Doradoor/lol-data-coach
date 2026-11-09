import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Rank, User, engine
from riot_client import obtain_puuid, obtain_rank

log = logging.getLogger(__name__)


def find_user(game_name: str, tagline: str) -> User | None:
    """Busca el usuario en la base local. Los Riot ID no distinguen mayúsculas."""
    name, tag = game_name.strip().casefold(), tagline.strip().casefold()
    with Session(engine) as session:
        for user in session.scalars(select(User)):
            if user.game_name.casefold() == name and user.tagline.casefold() == tag:
                return user
    return None


def register_user(game_name: str, tagline: str, platform: str = "la1", refresh_rank: bool = True) -> str:
    game_name, tagline = game_name.strip(), tagline.strip()
    user = find_user(game_name, tagline)

    if user:
        puuid = user.puuid
    else:
        # Solo le pedimos el puuid a Riot si no conocemos al jugador.
        puuid = obtain_puuid(game_name, tagline)
        with Session(engine) as session:
            if session.get(User, puuid) is None:
                session.add(User(puuid=puuid, game_name=game_name, tagline=tagline, platform=platform))
                session.commit()
                log.info("Usuario %s#%s registrado", game_name, tagline)

    if refresh_rank:
        save_current_rank(puuid, platform)
    return puuid


def get_current_rank(puuid: str, queue_type: str) -> Rank | None:
    with Session(engine) as session:
        stmt = (
            select(Rank)
            .where(Rank.puuid == puuid, Rank.queue_type == queue_type)
            .order_by(Rank.recorded_at.desc())
            .limit(1)
        )
        return session.scalars(stmt).first()


def save_current_rank(puuid: str, platform: str = "la1") -> None:
    rank_entries = obtain_rank(puuid, platform)
    now = int(time.time())

    with Session(engine) as session:
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
