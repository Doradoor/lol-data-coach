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
    """json con forma de match-v5 donde el jugador de prueba es el mid del equipo 100."""
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


class FakeClock:
    """reloj falso para probar el limitador sin esperar de verdad."""
    def __init__(self):
        """empieza en el segundo 0 sin esperas registradas."""
        self.now = 0.0
        self.slept = []

    def __call__(self):
        """devuelve la hora falsa actual."""
        return self.now

    def sleep(self, seconds):
        """registra la espera y avanza la hora."""
        self.slept.append(seconds)
        self.now += seconds


def test_rate_limiter_does_not_wait_under_limit():
    """verifica que no espere mientras no se pase el limite."""
    clock = FakeClock()
    limiter = RateLimiter([(3, 1), (10, 120)], clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.acquire()
    assert clock.slept == []


def test_rate_limiter_waits_when_window_is_full():
    """verifica que espere lo justo cuando la ventana corta se llena."""
    clock = FakeClock()
    limiter = RateLimiter([(2, 1), (10, 120)], clock=clock, sleep=clock.sleep)
    limiter.acquire()
    clock.now = 0.3
    limiter.acquire()
    limiter.acquire()
    assert clock.slept == [pytest.approx(0.7)]


def test_rate_limiter_long_window():
    """verifica que respete la ventana larga."""
    clock = FakeClock()
    limiter = RateLimiter([(100, 1), (3, 120)], clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.acquire()
    limiter.acquire()
    assert sum(clock.slept) == pytest.approx(120)


def test_memory_engine_is_isolated_from_disk():
    """verifica que la base en memoria no toque la del disco."""
    disk_before = Session(engine).scalar(select(func.count()).select_from(Match))

    mem = create_memory_engine()
    save_match(fake_match("LA1_TEST_1", 1), mem)
    assert Session(mem).scalar(select(func.count()).select_from(Match)) == 1
    assert Session(engine).scalar(select(func.count()).select_from(Match)) == disk_before
    mem.dispose()


def test_dispose_frees_memory_database():
    """verifica que dispose borre la base en memoria."""
    mem = create_memory_engine()
    save_match(fake_match("LA1_TEST_1", 1), mem)
    mem.dispose()
    with pytest.raises(OperationalError):
        with mem.connect() as conn:
            conn.execute(text("SELECT count(*) FROM matches"))


@pytest.fixture
def fake_riot(monkeypatch):
    """reemplaza las llamadas a riot de live.py y registra con que parametros se llamaron."""
    matches = {f"LA1_{i}": fake_match(f"LA1_{i}", i, i_win=i % 3 != 0) for i in range(30)}

    def fake_fetch(match_ids):
        """entrega las partidas falsas y un error para la partida rota."""
        for match_id in match_ids:
            if match_id == "LA1_ROTA":
                yield match_id, None, RiotAPIError("500")
            else:
                yield match_id, matches[match_id], None

    calls = {}

    def fake_puuid(name, tag, routing):
        """devuelve el puuid de prueba y guarda el routing usado."""
        calls["puuid_routing"] = routing
        return ME

    def fake_rank(puuid, platform):
        """devuelve un rank de prueba y guarda la plataforma usada."""
        calls["rank_platform"] = platform
        return [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II",
                 "leaguePoints": 40, "wins": 10, "losses": 8}]

    def fake_match_ids(puuid, count, queue, routing):
        """devuelve los ids de prueba y guarda la cola y el routing."""
        calls["queue"], calls["ids_routing"] = queue, routing
        return list(matches)[:count] + ["LA1_ROTA"]

    monkeypatch.setattr(live, "obtain_puuid", fake_puuid)
    monkeypatch.setattr(live, "obtain_rank", fake_rank)
    monkeypatch.setattr(live, "obtain_match_id", fake_match_ids)
    monkeypatch.setattr(live, "fetch_matches", fake_fetch)
    return calls


def test_fetch_player_live_end_to_end(fake_riot):
    """verifica la consulta temporal completa sin tocar la base del disco."""
    progress = []
    player = live.fetch_player_live("Amigo", "LAN", count=30, progress=lambda i, n: progress.append((i, n)))

    assert (player.region, player.queue) == ("LAN", "SoloQ")
    assert fake_riot == {"puuid_routing": "americas", "rank_platform": "la1",
                         "queue": 420, "ids_routing": "americas"}
    assert (player.downloaded, player.failed) == (30, 1)
    assert progress[-1] == (31, 31)

    matchups = get_my_matchups(ME, player.db)
    assert len(matchups) == 30
    assert {m["rival_champion"] for m in matchups} == {"Syndra"}
    assert get_current_rank(ME, "RANKED_SOLO_5x5", player.db).tier == "GOLD"
    assert len(build_features(load_data(player.db))) == 300

    assert get_my_matchups(ME) == []

    live.release(player)


def test_fetch_player_live_releases_db_on_error(fake_riot, monkeypatch):
    """verifica que si riot falla a mitad se libere la base en memoria."""
    created = []
    real_create = live.create_memory_engine
    monkeypatch.setattr(live, "create_memory_engine", lambda: created.append(real_create()) or created[-1])

    def boom(puuid, platform):
        """simula un error de riot."""
        raise RiotAPIError("Error 403")
    monkeypatch.setattr(live, "obtain_rank", boom)

    with pytest.raises(RiotAPIError):
        live.fetch_player_live("Amigo", "LAN")
    with pytest.raises(OperationalError):
        with created[0].connect() as conn:
            conn.execute(text("SELECT count(*) FROM users"))


def test_fetch_player_live_uses_selected_region_and_queue(fake_riot):
    """verifica que se usen la region y la cola elegidas."""
    player = live.fetch_player_live("Amigo", "EUW", count=5, region="EUW", queue="Flex")
    assert fake_riot == {"puuid_routing": "europe", "rank_platform": "euw1",
                         "queue": 440, "ids_routing": "europe"}
    live.release(player)


def test_routing_for_match_uses_match_prefix():
    """verifica la region deducida del prefijo del id."""
    from riot_client import routing_for_match
    assert routing_for_match("LA1_123") == "americas"
    assert routing_for_match("EUW1_123") == "europe"
    assert routing_for_match("KR_123") == "asia"
    assert routing_for_match("OC1_123") == "sea"
    assert routing_for_match("RARO_1") == "americas"


def test_every_region_has_known_routing():
    """verifica que todas las regiones usen un routing valido."""
    from riot_client import REGIONS
    assert {routing for _, routing in REGIONS.values()} == {"americas", "europe", "asia", "sea"}


def test_migrate_puuid_moves_all_rows():
    """verifica que el cambio de puuid mueva usuario, rank y partidas."""
    from models import Rank, User
    from users import migrate_puuid, store_rank

    mem = create_memory_engine()
    with Session(mem) as s:
        s.add(User(puuid=ME, game_name="Amigo", tagline="LAN", platform="la1"))
        s.commit()
    store_rank(ME, [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II",
                     "leaguePoints": 1, "wins": 1, "losses": 1}], mem)
    save_match(fake_match("LA1_1", 1), mem)

    migrate_puuid(ME, "puuid-nuevo", mem)

    assert get_my_matchups(ME, mem) == []
    assert len(get_my_matchups("puuid-nuevo", mem)) == 1
    assert get_current_rank("puuid-nuevo", "RANKED_SOLO_5x5", mem).tier == "GOLD"
    assert Session(mem).get(User, "puuid-nuevo").game_name == "Amigo"
    mem.dispose()


def fake_history(monkeypatch, total_matches: int) -> list[dict]:
    """simula un historial de partidas y devuelve los parametros de cada llamada."""
    import riot_client
    calls = []

    def fake_get(url, params=None):
        """devuelve una pagina de ids segun start y count."""
        calls.append(params)
        start, count = params["start"], params["count"]
        assert count <= 100
        return [f"LA1_{i}" for i in range(start, min(start + count, total_matches))]

    monkeypatch.setattr(riot_client, "riot_get", fake_get)
    return calls


def test_obtain_match_id_paginates_past_100(monkeypatch):
    """verifica que pida las partidas por paginas de 100."""
    from riot_client import obtain_match_id
    calls = fake_history(monkeypatch, total_matches=1000)
    ids = obtain_match_id("p", count=250)
    assert len(ids) == 250 and len(set(ids)) == 250
    assert [(c["start"], c["count"]) for c in calls] == [(0, 100), (100, 100), (200, 50)]


def test_obtain_match_id_stops_when_history_ends(monkeypatch):
    """verifica que se detenga cuando no hay mas partidas."""
    from riot_client import obtain_match_id
    calls = fake_history(monkeypatch, total_matches=130)
    assert len(obtain_match_id("p", count=500)) == 130
    assert len(calls) == 2


def test_estimated_download_minutes():
    """verifica el calculo del tiempo estimado de descarga."""
    from riot_client import estimated_download_minutes
    assert estimated_download_minutes(40) == 0
    assert estimated_download_minutes(1000) == pytest.approx((1000 - 95) / 95 * 2)


def test_sync_summary_messages():
    """verifica los mensajes del resumen de sincronizacion."""
    from sync_matches import SyncResult
    assert "ya estaban todas guardadas" in SyncResult("p", 300, 142, 142, 0, 0).summary()
    assert "Riot tiene 142 partidas" in SyncResult("p", 300, 142, 142, 0, 0).summary()
    assert "Revisé tus últimas 100" in SyncResult("p", 100, 100, 90, 10, 0).summary()
    assert "10 nuevas guardadas" in SyncResult("p", 100, 100, 90, 10, 0).summary()
    assert "2 no se pudieron descargar" in SyncResult("p", 100, 100, 90, 8, 2).summary()
    assert "no tiene partidas" in SyncResult("p", 100, 0, 0, 0, 0).summary()
