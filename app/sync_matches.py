import logging
import time
from collections.abc import Callable

from riot_client import RiotAPIError, obtain_match_data, obtain_match_id
from save_match import existing_match_ids, save_match
from users import register_user

log = logging.getLogger(__name__)

# La clave de desarrollo permite 100 llamadas cada 2 minutos: 1.2s entre llamadas no pasa el límite.
# Si igual llega un 429, riot_get espera lo que diga Riot y reintenta.
DELAY_BETWEEN_CALLS = 1.2


def sync_my_matches(
    game_name: str,
    tagline: str,
    count: int = 20,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    puuid = register_user(game_name, tagline)
    match_ids = obtain_match_id(puuid, count=count)

    # No descargar las que ya tenemos.
    already = existing_match_ids(match_ids)
    pending = [m for m in match_ids if m not in already]
    log.info("%d partidas encontradas, %d ya guardadas, %d por descargar",
             len(match_ids), len(already), len(pending))

    saved = 0
    for i, match_id in enumerate(pending, start=1):
        try:
            if save_match(obtain_match_data(match_id)):
                saved += 1
        except RiotAPIError as e:
            log.warning("Salté %s: %s", match_id, e)
        if progress:
            progress(i, len(pending))
        if i < len(pending):
            time.sleep(DELAY_BETWEEN_CALLS)

    log.info("Sincronizadas %d partidas nuevas (%d ya estaban)", saved, len(already))
    return puuid


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sync_my_matches("Níño", "1204", count=100)
