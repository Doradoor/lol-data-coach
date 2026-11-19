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

    calls = {}

    def fake_puuid(name, tag, routing):
        calls["puuid_routing"] = routing
        return ME

    def fake_rank(puuid, platform):
        calls["rank_platform"] = platform
        return [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II",
                 "leaguePoints": 40, "wins": 10, "losses": 8}]

    def fake_match_ids(puuid, count, queue, routing):
        calls["queue"], calls["ids_routing"] = queue, routing
        return list(matches)[:count] + ["LA1_ROTA"]

    monkeypatch.setattr(live, "obtain_puuid", fake_puuid)
    monkeypatch.setattr(live, "obtain_rank", fake_rank)
    monkeypatch.setattr(live, "obtain_match_id", fake_match_ids)
    monkeypatch.setattr(live, "fetch_matches", fake_fetch)
    return calls


def test_fetch_player_live_end_to_end(fake_riot):
    progress = []
    player = live.fetch_player_live("Amigo", "LAN", count=30, progress=lambda i, n: progress.append((i, n)))

    assert (player.region, player.queue) == ("LAN", "SoloQ")
    assert fake_riot == {"puuid_routing": "americas", "rank_platform": "la1",
                         "queue": 420, "ids_routing": "americas"}
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


def test_fetch_player_live_uses_selected_region_and_queue(fake_riot):
    player = live.fetch_player_live("Amigo", "EUW", count=5, region="EUW", queue="Flex")
    assert fake_riot == {"puuid_routing": "europe", "rank_platform": "euw1",
                         "queue": 440, "ids_routing": "europe"}
    live.release(player)


# ---------- Regiones ----------
def test_routing_for_match_uses_match_prefix():
    from riot_client import routing_for_match
    assert routing_for_match("LA1_123") == "americas"
    assert routing_for_match("EUW1_123") == "europe"
    assert routing_for_match("KR_123") == "asia"
    assert routing_for_match("OC1_123") == "sea"
    assert routing_for_match("RARO_1") == "americas"  # desconocido: el valor por defecto


def test_every_region_has_known_routing():
    from riot_client import REGIONS
    assert {routing for _, routing in REGIONS.values()} == {"americas", "europe", "asia", "sea"}
