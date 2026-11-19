import json
import logging

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from models import DATA_DIR, Match, Participant, engine

log = logging.getLogger(__name__)


def existing_match_ids(match_ids: list[str], db: Engine = engine) -> set[str]:
    """devuelve cuales de esos ids ya estan guardados en la base."""
    if not match_ids:
        return set()
    with Session(db) as session:
        stmt = select(Match.match_id).where(Match.match_id.in_(match_ids))
        return set(session.scalars(stmt))


def save_match(data: dict, db: Engine = engine) -> bool:
    """guarda la partida y sus 10 jugadores; devuelve false si ya existia."""
    info = data["info"]
    match_id = data["metadata"]["matchId"]

    participants = [
        Participant(
            match_id=match_id,
            puuid=player["puuid"],
            champion_name=player["championName"],
            team_position=player["teamPosition"],
            team_id=player["teamId"],
            win=player["win"],
            kills=player["kills"],
            deaths=player["deaths"],
            assists=player["assists"],
            gold_earned=player["goldEarned"],
            damage_dealt=player["totalDamageDealtToChampions"],
            damage_taken=player["totalDamageTaken"],
            heals_on_teammates=player["totalHealsOnTeammates"],
            vision_score=player["visionScore"],
            cs=player["totalMinionsKilled"] + player["neutralMinionsKilled"],
            kill_participation=player.get("challenges", {}).get("killParticipation"),
        )
        for player in info["participants"]
    ]

    with Session(db) as session:
        if session.get(Match, match_id) is not None:
            log.info("La partida %s ya está guardada, la salto", match_id)
            return False

        session.add(Match(
            match_id=match_id,
            game_version=info["gameVersion"],
            game_duration=info["gameDuration"],
            game_creation=info["gameCreation"],
            game_mode=info["gameMode"],
            end_game_result=info.get("endOfGameResult", "Unknown"),
        ))
        session.add_all(participants)
        session.commit()

    log.info("Guardé la partida %s con %d jugadores", match_id, len(participants))
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = json.loads((DATA_DIR / "match_data.json").read_text(encoding="utf-8"))
    save_match(data)
