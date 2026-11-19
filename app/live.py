"""consulta temporal: descarga y analiza a un jugador en una base en memoria, sin guardar nada en disco."""
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from models import User, create_memory_engine
from riot_client import REGIONS, fetch_matches, obtain_match_id, obtain_puuid, obtain_rank
from save_match import save_match
from users import store_rank

log = logging.getLogger(__name__)

QUEUES = {"SoloQ": 420, "Flex": 440}


@dataclass
class LivePlayer:
    """jugador consultado de forma temporal y su base en memoria."""
    puuid: str
    game_name: str
    tagline: str
    region: str
    queue: str
    db: Engine
    downloaded: int
    failed: int

    @property
    def riot_id(self) -> str:
        """devuelve el riot id como nombre#tag."""
        return f"{self.game_name}#{self.tagline}"


def fetch_player_live(
    game_name: str,
    tagline: str,
    count: int = 40,
    region: str = "LAN",
    queue: str = "SoloQ",
    progress: Callable[[int, int], None] | None = None,
) -> LivePlayer:
    """descarga el rank y las ultimas partidas del jugador a una base en memoria."""
    game_name, tagline = game_name.strip(), tagline.strip()
    platform, routing = REGIONS[region]
    puuid = obtain_puuid(game_name, tagline, routing=routing)

    db = create_memory_engine()
    try:
        with Session(db) as session:
            session.add(User(puuid=puuid, game_name=game_name, tagline=tagline, platform=platform))
            session.commit()
        store_rank(puuid, obtain_rank(puuid, platform), db)

        match_ids = obtain_match_id(puuid, count=count, queue=QUEUES[queue], routing=routing)
        downloaded = failed = 0
        for i, (match_id, data, error) in enumerate(fetch_matches(match_ids), start=1):
            if error:
                log.warning("Salté %s: %s", match_id, error)
                failed += 1
            else:
                save_match(data, db)
                downloaded += 1
            if progress:
                progress(i, len(match_ids))
    except BaseException:
        db.dispose()
        raise

    log.info("Consulta temporal de %s#%s: %d partidas (%d fallidas)", game_name, tagline, downloaded, failed)
    return LivePlayer(puuid, game_name, tagline, region, queue, db, downloaded, failed)


def release(player: LivePlayer) -> None:
    """cierra la base en memoria del jugador y libera sus datos."""
    player.db.dispose()
    log.info("Liberados los datos temporales de %s", player.riot_id)
