import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
# Siempre cargar el .env del repo (lol-coach/.env), no importa desde qué carpeta ejecutes.
_env_path = ROOT_DIR / ".env"
load_dotenv(_env_path)

API_KEY = (os.getenv("RIOT_API_KEY") or "").strip().strip('"').strip("'")
if not API_KEY:
    raise RuntimeError(
        f"RIOT_API_KEY is not set. Add it to {_env_path} "
        "(get a key at https://developer.riotgames.com — dev keys expire every 24h)."
    )

base_url = "https://americas.api.riotgames.com"
MAX_RETRIES = 4

_client = httpx.Client(headers={"X-Riot-Token": API_KEY}, timeout=15)


class RiotAPIError(RuntimeError):
    """Error de la API de Riot con un mensaje legible para mostrar al usuario."""


def _retry_after(response: httpx.Response, attempt: int) -> float:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return 2 ** attempt


def riot_get(url: str, params: dict | None = None):
    for attempt in range(1, MAX_RETRIES + 1):
        last_try = attempt == MAX_RETRIES
        try:
            response = _client.get(url, params=params)
        except httpx.TransportError as e:
            if last_try:
                raise RiotAPIError(f"No pude conectar con Riot: {e}") from e
            log.warning("Error de red (%s), reintentando %d/%d...", e, attempt, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue

        status = response.status_code

        # Errores temporales (límite de llamadas o caída de Riot): esperar y reintentar.
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
            raise RiotAPIError(f"No encontrado: {url}")
        if status >= 400:
            raise RiotAPIError(f"Error {status}: {response.text}")

        return response.json()

    raise AssertionError("unreachable")


def obtain_puuid(game_name: str, tagline: str) -> str:
    url = f"{base_url}/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tagline)}"
    return riot_get(url)["puuid"]


def obtain_match_id(puuid: str, count: int = 10, queue: int = 420) -> list[str]:
    url = f"{base_url}/lol/match/v5/matches/by-puuid/{puuid}/ids"
    return riot_get(url, params={"count": count, "queue": queue})


def obtain_match_data(match_id: str) -> dict:
    return riot_get(f"{base_url}/lol/match/v5/matches/{match_id}")


def obtain_rank(puuid: str, platform: str = "la1") -> list[dict]:
    return riot_get(f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}")


def save_json(data: dict, path: Path) -> None:
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
