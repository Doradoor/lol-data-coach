import pandas as pd
import plotly.express as px
import streamlit as st
from google.genai import errors

from coach import ask_coach, build_prompt
from stats import calculate_champion_stats, calculate_stats, get_my_matchups, overall_winrate
from sync_matches import sync_my_matches
from users import get_current_rank, register_user

st.set_page_config(page_title="LoL Coach", layout="wide")
st.title("LoL Coach: tus stats, no el promedio")


@st.cache_data(show_spinner=False)
def load_matchups(puuid: str) -> list[dict]:
    """Streamlit vuelve a ejecutar todo el script con cada interacción: esto evita recalcular."""
    return get_my_matchups(puuid)


def parse_riot_id(text: str) -> tuple[str, str] | None:
    name, sep, tag = text.strip().partition("#")
    if not sep or not name.strip() or not tag.strip():
        return None
    return name.strip(), tag.strip()


def min_slider(label: str, max_value: int, default: int) -> int:
    # st.slider falla si min == max, así que con 1 partida no mostramos el slider.
    if max_value <= 1:
        return 1
    return st.slider(label, 1, max_value, min(default, max_value))


# ---------- Entrada ----------
riot_id = st.text_input("Riot ID", placeholder="Nombre#TAG")
col_a, col_b = st.columns(2)
sync_first = col_a.checkbox("Descargar partidas nuevas de Riot (solo baja las que no tienes)")
count = col_b.number_input("Cuántas partidas revisar", 1, 100, 100)

if st.button("Analizar"):
    parsed = parse_riot_id(riot_id)
    if parsed is None:
        st.warning("Escribe tu Riot ID completo, con el tag: Nombre#TAG")
    else:
        game_name, tagline = parsed
        try:
            if sync_first:
                bar = st.progress(0.0, text="Buscando partidas...")
                puuid = sync_my_matches(
                    game_name, tagline, count=int(count),
                    progress=lambda i, n: bar.progress(i / n, text=f"Descargando partida {i}/{n}"),
                )
                bar.empty()
            else:
                with st.spinner("Buscando jugador..."):
                    puuid = register_user(game_name, tagline)
            load_matchups.clear()

            # Si cambió el jugador, empezar un chat nuevo.
            if st.session_state.get("puuid") != puuid:
                st.session_state["chat"] = []
            st.session_state["puuid"] = puuid
            st.session_state["riot_id"] = f"{game_name}#{tagline}"
        except RuntimeError as e:
            st.error(str(e))

if "puuid" not in st.session_state:
    st.stop()

# ---------- Resumen ----------
puuid = st.session_state["puuid"]
st.header(st.session_state["riot_id"])
matchups = load_matchups(puuid)

cols = st.columns(3)
for col, queue, label in [(cols[0], "RANKED_SOLO_5x5", "SoloQ"), (cols[1], "RANKED_FLEX_SR", "Flex")]:
    r = get_current_rank(puuid, queue)
    if r:
        col.metric(label, f"{r.tier} {r.rank_division}", f"{r.league_points} LP")
if matchups:
    cols[2].metric("Winrate (partidas analizadas)", f"{overall_winrate(matchups):.1%}", f"{len(matchups)} partidas")

champs = pd.DataFrame(calculate_champion_stats(matchups))
if champs.empty:
    st.warning("No hay partidas CLASSIC guardadas. Marca 'Descargar partidas'.")
    st.stop()

# ---------- Tendencia ----------
st.subheader("Tendencia")
games = pd.DataFrame(matchups)
games["fecha"] = pd.to_datetime(games["game_creation"], unit="ms")
games["kda"] = (games["kills"] + games["assists"]) / games["deaths"].clip(lower=1)
window = min(10, len(games))
games["winrate_movil"] = games["won"].astype(float).rolling(window, min_periods=1).mean() * 100
games["kda_movil"] = games["kda"].rolling(window, min_periods=1).mean()

t1, t2 = st.columns(2)
fig_wr = px.line(games, x="fecha", y="winrate_movil", markers=True,
                 labels={"winrate_movil": f"Winrate últimas {window} (%)", "fecha": ""},
                 hover_data=["my_champion", "rival_champion", "won"])
fig_wr.add_hline(y=50, line_dash="dot", opacity=0.5)
t1.plotly_chart(fig_wr, width="stretch")
t2.plotly_chart(
    px.line(games, x="fecha", y="kda_movil", markers=True,
            labels={"kda_movil": f"KDA promedio últimas {window}", "fecha": ""},
            hover_data=["my_champion", "rival_champion"]),
    width="stretch",
)

# ---------- Campeones ----------
st.subheader("Rendimiento por campeón")
min_games = min_slider("Mínimo de partidas por campeón", int(champs["total"].max()), 3)
view = champs[champs["total"] >= min_games].sort_values("total", ascending=False)
st.dataframe(
    view[["my_champion", "total", "wins", "adjusted_winrate", "avg_kda_text", "cs_per_min",
          "damage_share", "gold_share", "kill_participation"]],
    width="stretch",
    hide_index=True,
)
st.bar_chart(view.set_index("my_champion")["adjusted_winrate"])

# ---------- Matchups ----------
st.subheader("Mapa de calor de matchups")
mu = pd.DataFrame(calculate_stats(matchups))
min_mu = min_slider("Mínimo de partidas por matchup", int(mu["total"].max()), 1)
mu = mu[mu["total"] >= min_mu]

if mu.empty:
    st.info("Ningún matchup llega a ese mínimo. Baja el filtro.")
else:
    pivot = mu.pivot_table(index="my_champion", columns="rival_champion", values="adjusted_winrate")
    fig = px.imshow(
        pivot, color_continuous_scale="RdYlGn", zmin=35, zmax=65,
        aspect="auto", labels={"color": "Winrate ajustado %"},
    )
    st.plotly_chart(fig, width="stretch")

# ---------- Coach ----------
st.subheader("Pregúntale al coach")
chat = st.session_state.setdefault("chat", [])

for msg in chat:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])

question = st.chat_input("¿Cómo juego la línea, qué campeón elijo...?")
if question:
    with st.chat_message("user"):
        st.write(question)

    game_name, tagline = st.session_state["riot_id"].split("#", 1)
    context = build_prompt(game_name, tagline, puuid, matchups)

    with st.chat_message("assistant"):
        with st.spinner("El coach está pensando..."):
            try:
                answer = ask_coach(context, question, history=chat)
            except errors.ServerError:
                answer = "Gemini está saturado en este momento. Intenta de nuevo en un minuto."
            except errors.ClientError as e:
                if e.code == 429:
                    answer = "Se agotó la cuota diaria gratuita del coach. Intenta más tarde."
                else:
                    answer = f"Error del coach: {e}"
            except RuntimeError as e:
                answer = str(e)
        st.write(answer)

    chat.append({"role": "user", "content": question})
    chat.append({"role": "assistant", "content": answer})
