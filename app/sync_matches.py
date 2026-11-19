import logging
from collections.abc import Callable
from dataclasses import dataclass

from riot_client import fetch_matches, obtain_match_id
from save_match import existing_match_ids, save_match
from users import register_user

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    puuid: str
    requested: int   # cuántas partidas se pidieron
    found: int       # cuántas tiene Riot en el historial (puede ser menos de las pedidas)
    already: int     # cuántas ya estaban guardadas
    saved: int       # cuántas nuevas se guardaron
    failed: int      # cuántas no se pudieron descargar

    def summary(self) -> str:
        if self.found == 0:
            return "Riot no tiene partidas de esta cola en tu historial."
        history = (f"Riot tiene {self.found} partidas en tu historial"
                   if self.found < self.requested else f"Revisé tus últimas {self.found} partidas")
        if self.saved == 0 and self.failed == 0:
            return f"{history} y ya estaban todas guardadas: no hay partidas nuevas."
        text = f"{history}: {self.saved} nuevas guardadas, {self.already} ya estaban."
        if self.failed:
            text += f" {self.failed} no se pudieron descargar; vuelve a intentarlo más tarde."
        return text


def sync_my_matches(
    game_name: str,
    tagline: str,
    count: int = 20,
    progress: Callable[[int, int], None] | None = None,
) -> SyncResult:
    """Descarga las partidas nuevas del jugador y las GUARDA en data/lol.db."""
    puuid = register_user(game_name, tagline)
    match_ids = obtain_match_id(puuid, count=count)

    # No descargar las que ya tenemos.
    already = existing_match_ids(match_ids)
    pending = [m for m in match_ids if m not in already]
    log.info("%d partidas encontradas, %d ya guardadas, %d por descargar",
             len(match_ids), len(already), len(pending))

    # El ritmo de las llamadas lo controla el RateLimiter de riot_client.
    saved = failed = 0
    for i, (match_id, data, error) in enumerate(fetch_matches(pending), start=1):
        if error:
            log.warning("Salté %s: %s", match_id, error)
            failed += 1
        elif save_match(data):
            saved += 1
        if progress:
            progress(i, len(pending))

    result = SyncResult(puuid, count, len(match_ids), len(already), saved, failed)
    log.info(result.summary())
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sync_my_matches("Níño", "1204", count=100)
