from types import SimpleNamespace

import pytest

from stats import (
    avg,
    bayesian_rating,
    calculate_champion_stats,
    calculate_stats,
    find_rival,
    kda_ratio,
    overall_winrate,
    safe_div,
)


def make_matchup(champ="Ahri", rival="Syndra", won=True, k=5, d=2, a=7, **extra):
    """matchup de prueba con valores por defecto."""
    base = {
        "my_champion": champ, "rival_champion": rival, "won": won, "role": "MIDDLE",
        "kills": k, "deaths": d, "assists": a,
        "damage_share": 0.3, "gold_share": 0.25, "damage_vs_rival": 1.2,
        "gold_vs_rival": 1.1, "vision_vs_rival": 0.9, "heals_on_teammates": 0,
        "kill_participation": 0.6, "cs_per_min": 7.5,
    }
    return base | extra


def test_safe_div():
    """verifica la division segura."""
    assert safe_div(10, 4) == 2.5
    assert safe_div(10, 0) is None
    assert safe_div(None, 4) is None
    assert safe_div(0, 4) == 0


def test_avg_ignores_none():
    """verifica que el promedio ignore los none."""
    assert avg([1, None, 3]) == 2
    assert avg([None, None]) is None
    assert avg([]) is None


def test_kda_ratio_zero_deaths():
    """verifica el kda cuando no hay muertes."""
    assert kda_ratio(5, 0, 5) == 10
    assert kda_ratio(5, 2, 5) == 5


def test_bayesian_rating_pulls_small_samples_to_prior():
    """verifica que con pocas partidas el winrate se acerque al prior."""
    assert bayesian_rating(1, 1) == pytest.approx(6 / 11)
    assert bayesian_rating(0, 0, global_winrate=0.55) == pytest.approx(0.55)
    assert bayesian_rating(700, 1000) == pytest.approx(705 / 1010)


def test_overall_winrate():
    """verifica el winrate general."""
    assert overall_winrate([]) == 0.5
    games = [make_matchup(won=True), make_matchup(won=True), make_matchup(won=False), make_matchup(won=False)]
    assert overall_winrate(games) == 0.5
    assert overall_winrate(games[:3]) == pytest.approx(2 / 3)


def test_champion_stats_groups_and_averages():
    """verifica la agrupacion y los promedios por campeon."""
    games = [
        make_matchup("Ahri", won=True, k=10, d=2, a=4, cs_per_min=8.0),
        make_matchup("Ahri", won=False, k=2, d=6, a=2, cs_per_min=6.0),
        make_matchup("Zed", won=True),
    ]
    stats = {s["my_champion"]: s for s in calculate_champion_stats(games)}

    ahri = stats["Ahri"]
    assert (ahri["wins"], ahri["total"]) == (1, 2)
    assert ahri["avg_kda_text"] == "6.0/4.0/3.0"
    assert ahri["kda"] == pytest.approx(18 / 8, abs=0.01)
    assert ahri["cs_per_min"] == 7.0
    assert stats["Zed"]["total"] == 1


def test_adjusted_winrate_uses_personal_prior():
    """verifica que el ajuste use el winrate propio como prior."""
    games = [make_matchup("Ahri", won=True) for _ in range(8)] + [
        make_matchup("Ahri", won=False), make_matchup("Zed", won=False),
    ]
    zed = next(s for s in calculate_champion_stats(games) if s["my_champion"] == "Zed")
    expected = (0 + 10 * 0.8) / (1 + 10) * 100
    assert zed["adjusted_winrate"] == pytest.approx(expected, abs=0.1)


def test_matchup_stats_key_by_rival():
    """verifica que los matchups se separen por rival."""
    games = [make_matchup("Ahri", "Syndra"), make_matchup("Ahri", "Zed"), make_matchup("Ahri", "Zed")]
    by_rival = {s["rival_champion"]: s["total"] for s in calculate_stats(games)}
    assert by_rival == {"Syndra": 1, "Zed": 2}


def test_find_rival():
    """verifica la busqueda del rival de linea."""
    me = SimpleNamespace(team_position="MIDDLE", team_id=100)
    ally_mid = SimpleNamespace(team_position="MIDDLE", team_id=100)
    enemy_top = SimpleNamespace(team_position="TOP", team_id=200)
    enemy_mid = SimpleNamespace(team_position="MIDDLE", team_id=200)

    assert find_rival(me, [me, ally_mid, enemy_top, enemy_mid]) is enemy_mid
    assert find_rival(me, [me, enemy_top]) is None
    no_role = SimpleNamespace(team_position="", team_id=100)
    assert find_rival(no_role, [no_role, SimpleNamespace(team_position="", team_id=200)]) is None


def test_rival_stats_groups_by_rival_across_my_champions():
    """verifica las stats por rival juntando todos mis campeones."""
    from stats import calculate_rival_stats
    games = [
        make_matchup("Ahri", "Zed", won=False),
        make_matchup("Syndra", "Zed", won=False),
        make_matchup("Ahri", "Lux", won=True),
        make_matchup("Ahri", "Lux", won=True),
    ]
    rivals = {r["rival_champion"]: r for r in calculate_rival_stats(games)}
    assert rivals["Zed"]["total"] == 2 and rivals["Zed"]["wins"] == 0
    assert rivals["Zed"]["vs_average"] < 0 < rivals["Lux"]["vs_average"]
    assert rivals["Zed"]["vs_average"] == pytest.approx(-8.3, abs=0.1)
