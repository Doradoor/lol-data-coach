"""que metricas separan victorias de derrotas en cada rol (regresion logistica).

uso:
    python analysis.py             diferencia contra tu rival de linea
    python analysis.py --sin-kda   lo mismo, sin kda ni participacion en kills
    python analysis.py --equipo    parte del total de tu equipo (predice poco)
"""
import argparse

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import text
from sqlalchemy.engine import Engine

from models import engine
from stats import MIN_GAME_SECONDS

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

TEAM_FEATURES = [
    "log_kda",
    "damage_share",
    "damage_taken_share",
    "vision_share",
    "heal_share",
    "cs_per_min",
    "kill_participation",
]

RIVAL_FEATURES = [
    "log_kda",
    "damage_per_min",
    "damage_taken_per_min",
    "vision_per_min",
    "heal_per_min",
    "cs_per_min",
    "kill_participation",
]


HEAL_FEATURES = {"heal_share", "heal_per_min"}
HEAL_ROLES = {"UTILITY"}

MIN_ROWS = 50


def load_data(db: Engine = engine) -> pd.DataFrame:
    """trae a los 10 jugadores de cada partida valida de la base db."""
    query = text("""
        SELECT p.*, m.game_duration
        FROM participants p
        JOIN matches m ON p.match_id = m.match_id
        WHERE m.game_mode = 'CLASSIC'
          AND m.game_duration > :min_duration
          AND m.end_game_result = 'GameComplete'
    """)
    return pd.read_sql(query, db, params={"min_duration": MIN_GAME_SECONDS})


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """calcula las metricas por jugador: partes del equipo, valores por minuto y log del kda."""
    df = df.copy()
    team = df.groupby(["match_id", "team_id"])
    minutes = df["game_duration"] / 60

    def share(col: str) -> pd.Series:
        """parte del total del equipo para una columna (0 si el equipo suma 0)."""
        team_sum = team[col].transform("sum")
        return (df[col] / team_sum.replace(0, np.nan)).fillna(0)

    kda = (df["kills"] + df["assists"]) / df["deaths"].clip(lower=1)
    df["log_kda"] = np.log1p(kda)

    df["damage_share"] = share("damage_dealt")
    df["damage_taken_share"] = share("damage_taken")
    df["vision_share"] = share("vision_score")
    df["heal_share"] = share("heals_on_teammates")

    df["damage_per_min"] = df["damage_dealt"] / minutes
    df["damage_taken_per_min"] = df["damage_taken"] / minutes
    df["vision_per_min"] = df["vision_score"] / minutes
    df["heal_per_min"] = df["heals_on_teammates"] / minutes
    df["cs_per_min"] = df["cs"] / minutes

    df["kill_participation"] = pd.to_numeric(df["kill_participation"], errors="coerce")
    df["win"] = df["win"].astype(int)
    return df


def vs_rival(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    """reemplaza cada metrica por la diferencia contra el rival de linea de la misma partida."""
    pairs = df.groupby(["match_id", "team_position"])
    valid = (pairs["win"].transform("size") == 2) & (pairs["win"].transform("sum") == 1)
    df = df[valid & (df["team_position"] != "")].copy()

    pairs = df.groupby(["match_id", "team_position"])
    for col in features:
        df[col] = 2 * df[col] - pairs[col].transform("sum")
    return df


def features_for(role: str, base: list[str], drop_kda: bool, exclude: frozenset[str] = frozenset()) -> list[str]:
    """devuelve las metricas a usar en un rol (curacion solo en support, sin las excluidas)."""
    return [
        f for f in base
        if not (drop_kda and f == "log_kda")
        and f not in exclude
        and not (f in HEAL_FEATURES and role not in HEAL_ROLES)
    ]


def fit_role(df: pd.DataFrame, role: str, features: list[str], paired: bool = False) -> dict | None:
    """entrena la regresion logistica de un rol y devuelve filas, exactitud y coeficientes (none si hay pocas filas)."""
    d = df[df["team_position"] == role].dropna(subset=features)
    if len(d) < MIN_ROWS:
        return None

    model = make_pipeline(StandardScaler(), LogisticRegression(fit_intercept=not paired))

    cv = GroupKFold(n_splits=5)
    scores = cross_val_score(model, d[features], d["win"], cv=cv, groups=d["match_id"])

    model.fit(d[features], d["win"])
    coefs = model.named_steps["logisticregression"].coef_[0]

    return {
        "rows": len(d),
        "accuracy": float(scores.mean()),
        "accuracy_std": float(scores.std()),
        "coefs": {name: float(c) for name, c in zip(features, coefs)},
    }


def analyze_role(df: pd.DataFrame, role: str, features: list[str], paired: bool) -> None:
    """entrena el modelo de un rol e imprime su exactitud y sus coeficientes."""
    result = fit_role(df, role, features, paired)
    if result is None:
        rows = len(df[df["team_position"] == role].dropna(subset=features))
        print(f"{role}: solo {rows} filas, muestra insuficiente\n")
        return

    print(f"=== {role} ({result['rows']} filas) | exactitud: "
          f"{result['accuracy']:.0%} (±{result['accuracy_std']:.0%}) ===")
    for name, c in sorted(result["coefs"].items(), key=lambda x: abs(x[1]), reverse=True):
        print(f"  {name:22s} {c:+.2f}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--equipo", action="store_true",
                        help="usar la parte del total del equipo en vez de la diferencia contra el rival")
    parser.add_argument("--vs-rival", action="store_true",
                        help="(ya es el modo por defecto; se acepta para no romper comandos viejos)")
    parser.add_argument("--sin-kda", action="store_true",
                        help="excluir KDA y participación en kills, igual que el dashboard")
    args = parser.parse_args()

    paired = not args.equipo
    base = RIVAL_FEATURES if paired else TEAM_FEATURES
    excluded = frozenset({"log_kda", "kill_participation"}) if args.sin_kda else frozenset()

    data = build_features(load_data())
    if paired:
        data = vs_rival(data, base)

    print(f"Filas totales: {len(data)} | winrate base: {data['win'].mean():.0%}")
    print(f"Modo: {'diferencia contra el rival de línea' if paired else 'parte del total del equipo'}"
          f"{' | sin KDA ni participación en kills' if args.sin_kda else ''}")
    print("Exactitud = % de partidas acertadas en datos que el modelo no vio (azar = 50%).")
    print("Coeficiente > 0: más de eso se asocia con ganar; < 0: con perder (con el resto igual).\n")
    for role in ROLES:
        analyze_role(data, role, features_for(role, base, drop_kda=False, exclude=excluded), paired=paired)
