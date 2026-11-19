"""resumen rapido de una partida guardada como json, sin usar la base de datos."""
import json
import sys
from pathlib import Path

from models import DATA_DIR


def find_player(players: list[dict], puuid: str) -> dict | None:
    """busca al jugador por puuid dentro de los participantes."""
    return next((p for p in players if p["puuid"] == puuid), None)


def find_rival(players: list[dict], me: dict) -> dict | None:
    """busca al rival de linea del jugador (none si no tiene posicion)."""
    if not me["teamPosition"]:
        return None
    return next(
        (p for p in players
         if p["teamPosition"] == me["teamPosition"] and p["teamId"] != me["teamId"]),
        None,
    )


def kda_text(p: dict) -> str:
    """devuelve el kda como texto k/d/a."""
    return f"{p['kills']}/{p['deaths']}/{p['assists']}"


def analyze_match(route: Path, puuid: str) -> None:
    """imprime el resumen del jugador y de su rival en una partida json."""
    match = json.loads(Path(route).read_text(encoding="utf-8"))
    info = match["info"]
    players = info["participants"]

    me = find_player(players, puuid)
    if me is None:
        raise ValueError(f"El jugador {puuid} no está en la partida {route}")

    minutes = info["gameDuration"] / 60
    cs = me["totalMinionsKilled"] + me["neutralMinionsKilled"]

    print(f"Partida {match['metadata']['matchId']} | {info['gameMode']} | {minutes:.0f} min")
    print(
        f"yo:    {me['championName']} ({me['teamPosition'] or 'sin rol'}) | KDA {kda_text(me)} | "
        f"CS/min {cs / minutes:.1f} | daño {me['totalDamageDealtToChampions']:,} | "
        f"{'Victoria' if me['win'] else 'Derrota'}"
    )

    rival = find_rival(players, me)
    if rival is None:
        print("rival: no hay rival de línea en esta partida")
        return

    rival_cs = rival["totalMinionsKilled"] + rival["neutralMinionsKilled"]
    print(
        f"rival: {rival['championName']} | KDA {kda_text(rival)} | "
        f"CS/min {rival_cs / minutes:.1f} | daño {rival['totalDamageDealtToChampions']:,}"
    )


def default_puuid() -> str:
    """devuelve el puuid del primer usuario registrado en la base."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from models import User, engine

    with Session(engine) as session:
        user = session.scalars(select(User)).first()
    if user is None:
        raise SystemExit("No hay usuarios registrados. Pasa el puuid: python analyze_match.py ruta.json <puuid>")
    return user.puuid


if __name__ == "__main__":
    route = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_DIR / "match_data.json"
    puuid = sys.argv[2] if len(sys.argv) > 2 else default_puuid()
    analyze_match(route, puuid)
