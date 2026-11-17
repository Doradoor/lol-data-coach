"""Consulta temporal: descarga y analiza a un jugador sin guardar nada en disco.

Las partidas se guardan en una base SQLite en memoria (models.create_memory_engine),
así que stats.py, analysis.py y coach.py funcionan igual que con data/lol.db, solo
cambiando el parámetro `db`. Al llamar a release() la base se cierra y la memoria se libera.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from models import User, create_memory_engine
from riot_client import fetch_matches, obtain_match_id, obtain_puuid, obtain_rank
from save_match import save_match
from users import store_rank

log = logging.getLogger(__name__)


@dataclass
class LivePlayer:
    puuid: str
    game_name: str
    tagline: str
    db: Engine          # base en memoria con sus partidas y su rank
    downloaded: int     # partidas descargadas bien
    failed: int         # partidas que Riot no devolvió

    @property
    def riot_id(self) -> str:
        return f"{self.game_name}#{self.tagline}"


def fetch_player_live(
    game_name: str,
    tagline: str,
    count: int = 40,
    platform: str = "la1",
    progress: Callable[[int, int], None] | None = None,
) -> LivePlayer:
    """Descarga rank y últimas `count` partidas de SoloQ del jugador a una base en memoria."""
    game_name, tagline = game_name.strip(), tagline.strip()
    puuid = obtain_puuid(game_name, tagline)

    db = create_memory_engine()
    try:
        with Session(db) as session:
            session.add(User(puuid=puuid, game_name=game_name, tagline=tagline, platform=platform))
            session.commit()
        store_rank(puuid, obtain_rank(puuid, platform), db)

        match_ids = obtain_match_id(puuid, count=count)
        downloaded = failed = 0
        for i, (match_id, data, error) in enumerate(fetch_matches(match_ids), start=1):
            if error:
                log.warning("Salté %s: %s", match_id, error)
                failed += 1
            else:
                # Se guardan solo las 10 filas que usamos; el JSON completo (~100 KB) se descarta.
                save_match(data, db)
                downloaded += 1
            if progress:
                progress(i, len(match_ids))
    except BaseException:
        db.dispose()  # si algo falla a mitad, no dejar la base en memoria colgando
        raise

    log.info("Consulta temporal de %s#%s: %d partidas (%d fallidas)", game_name, tagline, downloaded, failed)
    return LivePlayer(puuid, game_name, tagline, db, downloaded, failed)


def release(player: LivePlayer) -> None:
    """Cierra la base en memoria: SQLite libera todo lo que tenía."""
    player.db.dispose()
    log.info("Liberados los datos temporales de %s", player.riot_id)
