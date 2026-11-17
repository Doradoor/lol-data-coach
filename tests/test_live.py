import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import live
from analysis import build_features, load_data
from models import Match, create_memory_engine, engine
from riot_client import RateLimiter, RiotAPIError
from save_match import save_match
from stats import get_my_matchups
from users import get_current_rank

ME = "puuid-amigo"
POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]


def fake_match(match_id: str, i: int, i_win: bool = True) -> dict:
    """JSON con la forma de match-v5: 10 jugadores, yo soy el Mid del equipo 100."""
    participants = []
    for team in (100, 200):
        for pos in POSITIONS:
            me = team == 100 and pos == "MIDDLE"
            participants.append({
                "puuid": ME if me else f"{match_id}-{team}-{pos}",
                "championName": "Ahri" if me else "Syndra" if pos == "MIDDLE" else "X",
                "teamPosition": pos, "teamId": team, "win": (team == 100) == i_win,
                "kills": 5, "deaths": 2, "assists": 5, "goldEarned": 10000,
                "totalDamageDealtToChampions": 20000, "totalDamageTaken": 15000,
                "totalHealsOnTeammates": 0, "visionScore": 20,
                "totalMinionsKilled": 200, "neutralMinionsKilled": 0,
                "challenges": {"killParticipation": 0.5},
            })
    return {
        "metadata": {"matchId": match_id},
        "info": {"gameVersion": "15.1", "gameDuration": 1800, "gameCreation": 1_700_000_000_000 + i,
                 "gameMode": "CLASSIC", "endOfGameResult": "GameComplete", "participants": participants},
    }


# ---------- RateLimiter ----------
class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_rate_limiter_does_not_wait_under_limit():
    clock = FakeClock()
    limiter = RateLimiter([(3, 1), (10, 120)], clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.acquire()
    assert clock.slept == []


def test_rate_limiter_waits_when_window_is_full():
    clock = FakeClock()
    limiter = RateLimiter([(2, 1), (10, 120)], clock=clock, sleep=clock.sleep)
    limiter.acquire()
    clock.now = 0.3
    limiter.acquire()
    limiter.acquire()  # la ventana de 1 s está llena: espera a que salga la del instante 0
    assert clock.slept == [pytest.approx(0.7)]


def test_rate_limiter_long_window():
    clock = FakeClock()
    limiter = RateLimiter([(100, 1), (3, 120)], clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.acquire()
    limiter.acquire()
    assert sum(clock.slept) == pytest.approx(120)


# ---------- Base en memoria ----------
def test_memory_engine_is_isolated_from_disk():
    disk_before = Session(engine).scalar(select(func.count()).select_from(Match))

    mem = create_memory_engine()
    save_match(fake_match("LA1_TEST_1", 1), mem)
    assert Session(mem).scalar(select(func.count()).select_from(Match)) == 1
    assert Session(engine).scalar(select(func.count()).select_from(Match)) == disk_before
    mem.dispose()


def test_dispose_frees_memory_database():
    mem = create_memory_engine()
    save_match(fake_match("LA1_TEST_1", 1), mem)
    mem.dispose()
    # Tras dispose, la conexión vieja se cerró: una nueva ve una base vacía, sin tablas.
    with pytest.raises(OperationalError):
        with mem.connect() as conn:
            conn.execute(text("SELECT count(*) FROM matches"))


# ---------- fetch_player_live, sin llamar a Riot ----------
@pytest.fixture
def fake_riot(monkeypatch):
    matches = {f"LA1_{i}": fake_match(f"LA1_{i}", i, i_win=i % 3 != 0) for i in range(30)}

    def fake_fetch(match_ids):
        for match_id in match_ids:
            if match_id == "LA1_ROTA":
                yield match_id, None, RiotAPIError("500")
            else:
                yield match_id, matches[match_id], None

    monkeypatch.setattr(live, "obtain_puuid", lambda name, tag: ME)
    monkeypatch.setattr(live, "obtain_rank", lambda puuid, platform: [{
        "queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II",
        "leaguePoints": 40, "wins": 10, "losses": 8,
    }])
    monkeypatch.setattr(live, "obtain_match_id", lambda puuid, count: list(matches)[:count] + ["LA1_ROTA"])
    monkeypatch.setattr(live, "fetch_matches", fake_fetch)


def test_fetch_player_live_end_to_end(fake_riot):
    progress = []
    player = live.fetch_player_live("Amigo", "LAN", count=30, progress=lambda i, n: progress.append((i, n)))

    assert (player.downloaded, player.failed) == (30, 1)
    assert progress[-1] == (31, 31)

    # Las mismas funciones del modo normal funcionan con la base en memoria.
    matchups = get_my_matchups(ME, player.db)
    assert len(matchups) == 30
    assert {m["rival_champion"] for m in matchups} == {"Syndra"}
    assert get_current_rank(ME, "RANKED_SOLO_5x5", player.db).tier == "GOLD"
    assert len(build_features(load_data(player.db))) == 300

    # Y nada de eso está en la base del disco.
    assert get_my_matchups(ME) == []

    live.release(player)


def test_fetch_player_live_releases_db_on_error(fake_riot, monkeypatch):
    created = []
    real_create = live.create_memory_engine
    monkeypatch.setattr(live, "create_memory_engine", lambda: created.append(real_create()) or created[-1])

    def boom(puuid, platform):
        raise RiotAPIError("Error 403")
    monkeypatch.setattr(live, "obtain_rank", boom)

    with pytest.raises(RiotAPIError):
        live.fetch_player_live("Amigo", "LAN")
    with pytest.raises(OperationalError):  # la base quedó liberada
        with created[0].connect() as conn:
            conn.execute(text("SELECT count(*) FROM users"))
