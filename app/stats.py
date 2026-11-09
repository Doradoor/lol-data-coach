import logging
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Match, Participant, engine

log = logging.getLogger(__name__)

METRICS = [
    "damage_share", "gold_share",
    "damage_vs_rival", "gold_vs_rival", "vision_vs_rival",
    "heals_on_teammates", "kill_participation", "cs_per_min",
]

MIN_GAME_SECONDS = 300  # menos de 5 min = remake


def safe_div(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b else None


def avg(values: list) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 2) if clean else None


def kda_ratio(kills: int, deaths: int, assists: int) -> float:
    return (kills + assists) / max(deaths, 1)


def bayesian_rating(wins: int, total: int, global_winrate: float = 0.5, k: int = 10) -> float:
    """Winrate 'suavizado': con pocas partidas se acerca a global_winrate, con muchas al real."""
    return (wins + k * global_winrate) / (total + k)


def find_rival(me: Participant, match_players: list[Participant]) -> Participant | None:
    if not me.team_position:
        return None
    return next(
        (p for p in match_players
         if p.team_position == me.team_position and p.team_id != me.team_id),
        None,
    )


def get_my_matchups(puuid: str) -> list[dict]:
    """Una fila por partida válida, ordenadas de la más antigua a la más reciente."""
    matchups = []

    with Session(engine) as session:
        stmt = (
            select(Participant, Match)
            .join(Match, Participant.match_id == Match.match_id)
            .where(
                Participant.puuid == puuid,
                Match.game_mode == "CLASSIC",
                Match.game_duration > MIN_GAME_SECONDS,
                Match.end_game_result == "GameComplete",
            )
        )
        my_rows = session.execute(stmt).all()
        log.debug("Partidas encontradas (CLASSIC): %d", len(my_rows))

        # Cargar a los 10 jugadores de todas mis partidas en UNA consulta,
        # en vez de 3 consultas por partida.
        match_ids = [me.match_id for me, _ in my_rows]
        players_by_match: dict[str, list[Participant]] = defaultdict(list)
        if match_ids:
            for p in session.scalars(select(Participant).where(Participant.match_id.in_(match_ids))):
                players_by_match[p.match_id].append(p)

        for me, match in my_rows:
            players = players_by_match[me.match_id]
            rival = find_rival(me, players)
            if rival is None:
                log.debug("Sin rival en %s (team_position=%r)", me.match_id, me.team_position)
                continue

            team = [p for p in players if p.team_id == me.team_id]
            team_damage = sum(p.damage_dealt or 0 for p in team)
            team_gold = sum(p.gold_earned or 0 for p in team)

            matchups.append({
                "match_id": me.match_id,
                "game_creation": match.game_creation,
                "role": me.team_position,
                "my_champion": me.champion_name,
                "rival_champion": rival.champion_name,
                "won": bool(me.win),
                "kills": me.kills,
                "deaths": me.deaths,
                "assists": me.assists,
                "damage_share": safe_div(me.damage_dealt, team_damage),
                "gold_share": safe_div(me.gold_earned, team_gold),
                "damage_vs_rival": safe_div(me.damage_dealt, rival.damage_dealt),
                "gold_vs_rival": safe_div(me.gold_earned, rival.gold_earned),
                "heals_on_teammates": me.heals_on_teammates,
                "vision_vs_rival": safe_div(me.vision_score, rival.vision_score),
                "kill_participation": me.kill_participation,
                "cs_per_min": safe_div(me.cs, match.game_duration / 60),
            })

    matchups.sort(key=lambda m: m["game_creation"] or 0)
    return matchups


def overall_winrate(matchups: list[dict]) -> float:
    """Winrate general del jugador; 50% si todavía no hay partidas."""
    return safe_div(sum(m["won"] for m in matchups), len(matchups)) or 0.5


def main_roles(matchups: list[dict]) -> list[tuple[str, int]]:
    return Counter(m["role"] for m in matchups).most_common()


def group_by(matchups: list[dict], key_fn) -> dict:
    grouped = defaultdict(lambda: {
        "wins": 0, "total": 0, "kills": 0, "deaths": 0, "assists": 0,
        **{m: [] for m in METRICS},
    })

    for m in matchups:
        g = grouped[key_fn(m)]
        g["total"] += 1
        g["kills"] += m["kills"]
        g["deaths"] += m["deaths"]
        g["assists"] += m["assists"]
        if m["won"]:
            g["wins"] += 1
        for metric in METRICS:
            g[metric].append(m[metric])

    return grouped


def summarize(s: dict, prior: float = 0.5) -> dict:
    n = s["total"]
    result = {
        "wins": s["wins"],
        "total": n,
        "adjusted_winrate": round(bayesian_rating(s["wins"], n, prior) * 100, 1),
        "avg_kda_text": f"{s['kills']/n:.1f}/{s['deaths']/n:.1f}/{s['assists']/n:.1f}",
        "kda": round(kda_ratio(s["kills"], s["deaths"], s["assists"]), 2),
    }
    for metric in METRICS:
        result[metric] = avg(s[metric])
    return result


def calculate_stats(matchups: list[dict]) -> list[dict]:
    """Stats por matchup (mi campeón vs rival de línea).

    El winrate ajustado parte de TU winrate general, no de 50%: así un campeón
    con pocas partidas se compara contra tu nivel real.
    """
    prior = overall_winrate(matchups)
    grouped = group_by(matchups, lambda m: (m["my_champion"], m["rival_champion"]))
    return [
        {"my_champion": mine, "rival_champion": rival, **summarize(s, prior)}
        for (mine, rival), s in grouped.items()
    ]


def calculate_champion_stats(matchups: list[dict]) -> list[dict]:
    prior = overall_winrate(matchups)
    grouped = group_by(matchups, lambda m: m["my_champion"])
    return [
        {"my_champion": champ, **summarize(s, prior)}
        for champ, s in grouped.items()
    ]


if __name__ == "__main__":
    from users import register_user

    puuid = register_user("Níño", "1204", refresh_rank=False)
    matchups = get_my_matchups(puuid)
    print(f"{len(matchups)} partidas, winrate general {overall_winrate(matchups):.1%}")
    for s in sorted(calculate_champion_stats(matchups), key=lambda x: x["total"], reverse=True):
        print(
            f"{s['my_champion']}: {s['wins']}/{s['total']} → {s['adjusted_winrate']}% | "
            f"KDA {s['avg_kda_text']} | CS/min {s['cs_per_min']} | daño equipo {s['damage_share']} | "
            f"oro equipo {s['gold_share']} | daño vs rival {s['damage_vs_rival']}"
        )
