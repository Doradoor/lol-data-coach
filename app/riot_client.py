import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
_env_path = ROOT_DIR / ".env"
load_dotenv(_env_path)

API_KEY = (os.getenv("RIOT_API_KEY") or "").strip().strip('"').strip("'")
if not API_KEY:
    raise RuntimeError(
        f"RIOT_API_KEY is not set. Add it to {_env_path} "
        "(get a key at https://developer.riotgames.com — dev keys expire every 24h)."
    )

MAX_RETRIES = 4

REGIONS: dict[str, tuple[str, str]] = {
    "LAN": ("la1", "americas"),
    "LAS": ("la2", "americas"),
    "NA": ("na1", "americas"),
    "BR": ("br1", "americas"),
    "EUW": ("euw1", "europe"),
    "EUNE": ("eun1", "europe"),
    "TR": ("tr1", "europe"),
    "RU": ("ru", "europe"),
    "ME": ("me1", "europe"),
    "KR": ("kr", "asia"),
    "JP": ("jp1", "asia"),
    "OCE": ("oc1", "sea"),
    "SEA": ("sg2", "sea"),
    "TW": ("tw2", "sea"),
    "VN": ("vn2", "sea"),
}
_ROUTING_BY_PLATFORM = {platform: routing for platform, routing in REGIONS.values()}
DEFAULT_ROUTING = "americas"

_client = httpx.Client(headers={"X-Riot-Token": API_KEY}, timeout=15)


class RiotAPIError(RuntimeError):
    """error de la api de riot con un mensaje para mostrar al usuario."""


class RiotNotFoundError(RiotAPIError):
    """riot respondio 404: el jugador o la partida no existe."""


def regional_url(routing: str) -> str:
    """devuelve la url base de la api para un grupo regional."""
    return f"https://{routing}.api.riotgames.com"


def routing_for_match(match_id: str) -> str:
    """deduce el grupo regional a partir del prefijo del id de la partida."""
    platform = match_id.split("_", 1)[0].lower()
    return _ROUTING_BY_PLATFORM.get(platform, DEFAULT_ROUTING)


class RateLimiter:
    """limita las llamadas a riot con ventanas deslizantes; es seguro entre hilos."""

    def __init__(self, limits: list[tuple[int, float]],
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        """recibe los limites como [(llamadas, segundos), ...]."""
        self.limits = limits
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self._longest = max(seconds for _, seconds in limits)

    def acquire(self) -> None:
        """espera lo justo para no pasar ningun limite y registra la llamada."""
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self._longest:
                    self._calls.popleft()

                wait = 0.0
                for max_calls, seconds in self.limits:
                    recent = [t for t in self._calls if now - t < seconds]
                    if len(recent) >= max_calls:
                        wait = max(wait, recent[-max_calls] + seconds - now)

                if wait <= 0:
                    self._calls.append(now)
                    return
            self._sleep(wait)


_limiter = RateLimiter([(18, 1), (95, 120)])
DOWNLOAD_WORKERS = 4


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """segundos a esperar segun el header retry-after, o backoff exponencial si no viene."""
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return 2 ** attempt


def riot_get(url: str, params: dict | None = None):
    """hace un get a riot con limite de llamadas, reintentos y errores legibles."""
    for attempt in range(1, MAX_RETRIES + 1):
        last_try = attempt == MAX_RETRIES
        _limiter.acquire()
        try:
            response = _client.get(url, params=params)
        except httpx.TransportError as e:
            if last_try:
                raise RiotAPIError(f"No pude conectar con Riot: {e}") from e
            log.warning("Error de red (%s), reintentando %d/%d...", e, attempt, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue

        status = response.status_code

        if status == 429 or status >= 500:
            if last_try:
                raise RiotAPIError(f"Riot respondió {status} después de {MAX_RETRIES} intentos: {response.text}")
            wait = _retry_after(response, attempt) if status == 429 else 2 ** attempt
            log.warning("Riot respondió %d, espero %.0fs (%d/%d)...", status, wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            continue

        if status in (401, 403):
            raise RiotAPIError(
                f"Error {status}: {response.text}. Revisa RIOT_API_KEY en .env: las claves de "
                "desarrollo caducan cada 24h (genera una nueva en developer.riotgames.com)."
            )
        if status == 404:
            raise RiotNotFoundError(f"No encontrado: {url}")
        if status >= 400:
            raise RiotAPIError(f"Error {status}: {response.text}")

        return response.json()

    raise AssertionError("unreachable")


def obtain_puuid(game_name: str, tagline: str, routing: str = DEFAULT_ROUTING) -> str:
    """devuelve el puuid de un riot id."""
    url = f"{regional_url(routing)}/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tagline)}"
    try:
        return riot_get(url)["puuid"]
    except RiotNotFoundError:
        raise RiotNotFoundError(
            f"No encontramos a {game_name}#{tagline}. Revisa que el nombre y el tag estén bien escritos."
        ) from None


MATCH_IDS_PAGE = 100


def obtain_match_id(puuid: str, count: int = 10, queue: int = 420, routing: str = DEFAULT_ROUTING) -> list[str]:
    """devuelve los ids de las ultimas partidas, pidiendo paginas de 100."""
    url = f"{regional_url(routing)}/lol/match/v5/matches/by-puuid/{puuid}/ids"
    ids: list[str] = []
    while len(ids) < count:
        page_size = min(MATCH_IDS_PAGE, count - len(ids))
        page = riot_get(url, params={"start": len(ids), "count": page_size, "queue": queue})
        ids.extend(page)
        if len(page) < page_size:
            break
    return ids


def estimated_download_minutes(matches: int) -> float:
    """minutos aproximados para descargar esa cantidad de partidas."""
    max_calls, window = max(_limiter.limits, key=lambda limit: limit[1])
    return max(0, matches - max_calls) / max_calls * window / 60


def obtain_match_data(match_id: str) -> dict:
    """descarga el detalle de una partida desde su region."""
    return riot_get(f"{regional_url(routing_for_match(match_id))}/lol/match/v5/matches/{match_id}")


def fetch_matches(match_ids: list[str]) -> Iterator[tuple[str, dict | None, RiotAPIError | None]]:
    """descarga partidas en paralelo y las entrega a medida que llegan."""
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = {pool.submit(obtain_match_data, match_id): match_id for match_id in match_ids}
        for future in as_completed(futures):
            match_id = futures[future]
            try:
                yield match_id, future.result(), None
            except RiotAPIError as e:
                yield match_id, None, e


def obtain_rank(puuid: str, platform: str = "la1") -> list[dict]:
    """devuelve las entradas de rank del jugador (lista vacia si no tiene)."""
    try:
        return riot_get(f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}")
    except RiotNotFoundError:
        return []


def save_json(data: dict, path: Path) -> None:
    """guarda un diccionario como archivo json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    puuid = obtain_puuid("Níño", "1204")
    print(puuid)

    matches = obtain_match_id(puuid, count=20, queue=420)
    print(f"Found {len(matches)} matches")

    detail = obtain_match_data(matches[0])
    save_json(detail, ROOT_DIR / "data" / "match_data.json")
    print("Done")
