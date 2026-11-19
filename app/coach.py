import logging
import os
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors

from sqlalchemy.engine import Engine

from models import ROOT_DIR, engine
from stats import calculate_champion_stats, calculate_stats, get_my_matchups, main_roles, overall_winrate
from users import get_current_rank

log = logging.getLogger(__name__)

load_dotenv(ROOT_DIR / ".env")

MODEL = "gemini-3.8-flash"
MAX_HISTORY_MESSAGES = 8
RECENT_GAMES = 10

ROLE_NAMES = {"TOP": "Top", "JUNGLE": "Jungla", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}

_client: genai.Client | None = None


def get_client() -> genai.Client:
    """crea el cliente de gemini la primera vez que se usa."""
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(f"GEMINI_API_KEY no está configurada en {ROOT_DIR / '.env'}")
        _client = genai.Client(api_key=api_key)
    return _client


def fmt(v, suffix: str = "") -> str:
    """formatea un valor o devuelve n/d si no hay dato."""
    return "n/d" if v is None else f"{v}{suffix}"


def pct(v) -> str:
    """formatea una proporcion como porcentaje o devuelve n/d."""
    return "n/d" if v is None else f"{v * 100:.0f}%"


def build_prompt(
    game_name: str,
    tagline: str,
    puuid: str,
    matchups: list[dict] | None = None,
    db: Engine = engine,
) -> str:
    """arma el contexto con las stats del jugador que se le manda al coach."""
    if matchups is None:
        matchups = get_my_matchups(puuid, db)

    lines = [f"Jugador: {game_name}#{tagline}"]

    rank = get_current_rank(puuid, "RANKED_SOLO_5x5", db)
    if rank:
        lines.append(f"SoloQ: {rank.tier} {rank.rank_division}, {rank.league_points} LP "
                     f"({rank.wins}V/{rank.losses}D en la temporada)")

    if not matchups:
        lines.append("Todavía no hay partidas analizadas.")
        return "\n".join(lines)

    lines.append(f"Partidas analizadas: {len(matchups)}, winrate general {overall_winrate(matchups):.1%}")
    lines.append("Roles jugados: " + ", ".join(
        f"{ROLE_NAMES.get(role, role)} ({n})" for role, n in main_roles(matchups)
    ))

    recent = matchups[-RECENT_GAMES:]
    form = " ".join(f"{'V' if m['won'] else 'D'}:{m['my_champion']}" for m in recent)
    lines.append(f"Últimas {len(recent)} partidas (de la más antigua a la más reciente): {form}")

    lines.append(
        "Mis campeones (winrate ajustado, partidas, KDA promedio, CS/min, % del daño del equipo, "
        "% del oro del equipo, daño relativo al rival de línea, participación en kills):"
    )
    for s in sorted(calculate_champion_stats(matchups), key=lambda x: x["total"], reverse=True):
        lines.append(
            f"- {s['my_champion']}: {s['adjusted_winrate']}% ({s['wins']}/{s['total']}), "
            f"KDA {s['avg_kda_text']}, CS/min {fmt(s['cs_per_min'])}, "
            f"daño equipo {pct(s['damage_share'])}, oro equipo {pct(s['gold_share'])}, "
            f"daño vs rival {fmt(s['damage_vs_rival'], 'x')}, KP {pct(s['kill_participation'])}"
        )

    lines.append("Matchups de línea:")
    for s in sorted(calculate_stats(matchups), key=lambda x: x["total"], reverse=True):
        lines.append(
            f"- {s['my_champion']} vs {s['rival_champion']}: {s['adjusted_winrate']}% "
            f"({s['wins']}/{s['total']}), KDA {s['avg_kda_text']}, "
            f"oro vs rival {fmt(s['gold_vs_rival'], 'x')}"
        )

    return "\n".join(lines)


def ask_coach(context: str, question: str, history: list[dict] | None = None, retries: int = 3) -> str:
    """le pregunta a gemini con el contexto y el historial del chat, con reintentos."""
    conversation = ""
    if history:
        turns = [
            f"{'Jugador' if msg['role'] == 'user' else 'Coach'}: {msg['content']}"
            for msg in history[-MAX_HISTORY_MESSAGES:]
        ]
        conversation = "Conversación previa:\n" + "\n\n".join(turns) + "\n\n"

    prompt = f"""Eres un coach experto de League of Legends.
Aquí están las estadísticas reales del jugador:

{context}

{conversation}Pregunta del jugador: {question}

Da un consejo específico basado en estos datos, no consejos genéricos.
El "winrate ajustado" ya corrige por pocas partidas: no saques conclusiones fuertes de campeones o matchups con 1-2 partidas."""

    client = get_client()
    for attempt in range(1, retries + 1):
        try:
            response = client.models.generate_content(model=MODEL, contents=prompt)
            return response.text
        except errors.ServerError:
            if attempt == retries:
                raise
            log.warning("Gemini saturado, reintentando %d/%d...", attempt, retries)
            time.sleep(2 ** attempt)

    raise AssertionError("unreachable")


if __name__ == "__main__":
    from users import register_user

    puuid = register_user("Níño", "1204")
    contexto = build_prompt("Níño", "1204", puuid)
    print(contexto)
    print("---")
    print(ask_coach(contexto, "¿Con qué campeón me conviene entrar a la próxima partida y por qué?"))
