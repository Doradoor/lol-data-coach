from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Match, Participant, engine

if __name__ == "__main__":
    with Session(engine) as session:
        for m in session.scalars(select(Match)):
            print(m.match_id, m.game_mode, m.end_game_result, m.game_duration)

        total = session.scalar(select(func.count()).select_from(Participant))
        print(f"\nParticipantes guardados: {total}")
