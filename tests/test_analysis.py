import numpy as np
import pandas as pd
import pytest

from analysis import build_features, features_for, vs_rival, TEAM_FEATURES, RIVAL_FEATURES


def player(match, team, pos, win, k=2, d=2, a=2, dmg=1000, taken=1000, vision=10, heals=0, cs=100, kp=0.5):
    return {
        "match_id": match, "team_id": team, "team_position": pos, "win": win,
        "kills": k, "deaths": d, "assists": a, "damage_dealt": dmg, "damage_taken": taken,
        "vision_score": vision, "heals_on_teammates": heals, "cs": cs,
        "kill_participation": kp, "game_duration": 600,
    }


@pytest.fixture
def match_df():
    return pd.DataFrame([
        player("M1", 100, "MIDDLE", True, k=8, d=0, a=4, dmg=3000, cs=200),
        player("M1", 100, "TOP", True, dmg=1000),
        player("M1", 200, "MIDDLE", False, k=1, d=5, a=1, dmg=1000, cs=150),
        player("M1", 200, "TOP", False, dmg=0),
    ])


def test_build_features(match_df):
    f = build_features(match_df)
    mid = f[(f.team_id == 100) & (f.team_position == "MIDDLE")].iloc[0]
    assert mid["log_kda"] == pytest.approx(np.log1p(12))  # 0 muertes cuenta como 1
    assert mid["damage_share"] == pytest.approx(0.75)
    assert mid["cs_per_min"] == pytest.approx(20)
    # Nadie curó: la parte es 0, no NaN.
    assert (f["heal_share"] == 0).all()
    assert not f[TEAM_FEATURES + RIVAL_FEATURES].isna().any().any()


def test_vs_rival_is_my_value_minus_rival(match_df):
    f = vs_rival(build_features(match_df), ["cs_per_min", "damage_per_min"])
    mid = f[f.team_position == "MIDDLE"].set_index("team_id")
    assert mid.loc[100, "cs_per_min"] == pytest.approx(5)    # 20 - 15
    assert mid.loc[200, "cs_per_min"] == pytest.approx(-5)
    assert mid.loc[100, "damage_per_min"] == pytest.approx(200)


def test_vs_rival_drops_incomplete_pairs(match_df):
    extra = pd.DataFrame([player("M2", 100, "JUNGLE", True)])  # sin rival
    f = vs_rival(build_features(pd.concat([match_df, extra])), ["cs_per_min"])
    assert "M2" not in set(f.match_id)


def test_features_for_heal_only_support():
    assert "heal_share" not in features_for("TOP", TEAM_FEATURES, drop_kda=False)
    assert "heal_share" in features_for("UTILITY", TEAM_FEATURES, drop_kda=False)
    assert "log_kda" not in features_for("MIDDLE", RIVAL_FEATURES, drop_kda=True)
