import logging
from collections.abc import Callable

from riot_client import fetch_matches, obtain_match_id
from save_match import existing_match_ids, save_match
from users import register_user

log = logging.getLogger(__name__)


def sync_my_matches(
    game_name: str,
    tagline: str,
    count: int = 20,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """Descarga las partidas nuevas del jugador y las GUARDA en data/lol.db."""
    puuid = register_user(game_name, tagline)
    match_ids = obtain_match_id(puuid, count=count)

    # No descargar las que ya tenemos.
    already = existing_match_ids(match_ids)
    pending = [m for m in match_ids if m not in already]
    log.info("%d partidas encontradas, %d ya guardadas, %d por descargar",
             len(match_ids), len(already), len(pending))

    # El ritmo de las llamadas lo controla el RateLimiter de riot_client.
    saved = 0
    for i, (match_id, data, error) in enumerate(fetch_matches(pending), start=1):
        if error:
            log.warning("Salté %s: %s", match_id, error)
        elif save_match(data):
            saved += 1
        if progress:
            progress(i, len(pending))

    log.info("Sincronizadas %d partidas nuevas (%d ya estaban)", saved, len(already))
    return puuid


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sync_my_matches("Níño", "1204", count=100)
