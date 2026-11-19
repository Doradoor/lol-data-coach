import numpy as np
import pandas as pd
import pytest

from analysis import MIN_ROWS, build_features, features_for, fit_role, vs_rival, TEAM_FEATURES, RIVAL_FEATURES


def player(match, team, pos, win, k=2, d=2, a=2, dmg=1000, taken=1000, vision=10, heals=0, cs=100, kp=0.5):
    """fila de jugador con valores por defecto para armar partidas de prueba."""
    return {
        "match_id": match, "team_id": team, "team_position": pos, "win": win,
        "kills": k, "deaths": d, "assists": a, "damage_dealt": dmg, "damage_taken": taken,
        "vision_score": vision, "heals_on_teammates": heals, "cs": cs,
        "kill_participation": kp, "game_duration": 600,
    }


@pytest.fixture
def match_df():
    """partida de prueba con mid y top de ambos equipos."""
    return pd.DataFrame([
        player("M1", 100, "MIDDLE", True, k=8, d=0, a=4, dmg=3000, cs=200),
        player("M1", 100, "TOP", True, dmg=1000),
        player("M1", 200, "MIDDLE", False, k=1, d=5, a=1, dmg=1000, cs=150),
        player("M1", 200, "TOP", False, dmg=0),
    ])


def test_build_features(match_df):
    """verifica log del kda, partes del equipo, cs por minuto y que no haya nan."""
    f = build_features(match_df)
    mid = f[(f.team_id == 100) & (f.team_position == "MIDDLE")].iloc[0]
    assert mid["log_kda"] == pytest.approx(np.log1p(12))
    assert mid["damage_share"] == pytest.approx(0.75)
    assert mid["cs_per_min"] == pytest.approx(20)
    assert (f["heal_share"] == 0).all()
    assert not f[TEAM_FEATURES + RIVAL_FEATURES].isna().any().any()


def test_vs_rival_is_my_value_minus_rival(match_df):
    """verifica que cada metrica sea mi valor menos el del rival."""
    f = vs_rival(build_features(match_df), ["cs_per_min", "damage_per_min"])
    mid = f[f.team_position == "MIDDLE"].set_index("team_id")
    assert mid.loc[100, "cs_per_min"] == pytest.approx(5)
    assert mid.loc[200, "cs_per_min"] == pytest.approx(-5)
    assert mid.loc[100, "damage_per_min"] == pytest.approx(200)


def test_vs_rival_drops_incomplete_pairs(match_df):
    """verifica que se descarten los roles sin rival."""
    extra = pd.DataFrame([player("M2", 100, "JUNGLE", True)])
    f = vs_rival(build_features(pd.concat([match_df, extra])), ["cs_per_min"])
    assert "M2" not in set(f.match_id)


def test_features_for_heal_only_support():
    """verifica que la curacion solo se use en support."""
    assert "heal_share" not in features_for("TOP", TEAM_FEATURES, drop_kda=False)
    assert "heal_share" in features_for("UTILITY", TEAM_FEATURES, drop_kda=False)
    assert "log_kda" not in features_for("MIDDLE", RIVAL_FEATURES, drop_kda=True)


def test_features_for_exclude():
    """verifica que se quiten las metricas excluidas."""
    feats = features_for("MIDDLE", RIVAL_FEATURES, drop_kda=False,
                         exclude=frozenset({"log_kda", "kill_participation"}))
    assert "log_kda" not in feats and "kill_participation" not in feats
    assert "cs_per_min" in feats


def synthetic_role(n_matches: int) -> pd.DataFrame:
    """partidas de mid donde el ganador farmea un poco mas."""
    rng = np.random.default_rng(0)
    rows = []
    for i in range(n_matches):
        for win in (1, 0):
            rows.append({"match_id": f"M{i}", "team_position": "MIDDLE", "win": win,
                         "cs_per_min": 7 + win + rng.normal(0, 1), "vision_per_min": rng.normal(1, 0.2)})
    return pd.DataFrame(rows)


def test_fit_role_returns_none_with_few_rows():
    """verifica que con pocas filas no se entrene el modelo."""
    df = synthetic_role(MIN_ROWS // 2 - 1)
    assert fit_role(df, "MIDDLE", ["cs_per_min"]) is None


def test_fit_role_result_shape():
    """verifica la forma del resultado y que detecte que farmear mas ayuda."""
    result = fit_role(synthetic_role(60), "MIDDLE", ["cs_per_min", "vision_per_min"])
    assert result["rows"] == 120
    assert 0 <= result["accuracy"] <= 1 and result["accuracy_std"] >= 0
    assert list(result["coefs"]) == ["cs_per_min", "vision_per_min"]
    assert result["coefs"]["cs_per_min"] > 0
