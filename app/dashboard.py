import pandas as pd
import plotly.express as px
import streamlit as st
from google.genai import errors

from analysis import RIVAL_FEATURES, ROLES, build_features, features_for, fit_role, load_data, vs_rival
from coach import ask_coach, build_prompt
from live import fetch_player_live, release
from models import engine
from stats import calculate_champion_stats, calculate_stats, get_my_matchups, overall_winrate
from sync_matches import sync_my_matches
from users import get_current_rank, register_user

st.set_page_config(page_title="LoL Coach", layout="wide")
st.title("LoL Coach: tus stats, no el promedio")

ROLE_LABELS = {"TOP": "Top", "JUNGLE": "Jungla", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
METRIC_LABELS = {
    "log_kda": "KDA (escala log)",
    "damage_per_min": "Daño a campeones / min",
    "damage_taken_per_min": "Daño recibido / min",
    "vision_per_min": "Visión / min",
    "heal_per_min": "Curación a aliados / min",
    "cs_per_min": "CS / min",
    "kill_participation": "Participación en kills",
}
# Métricas que se calculan con kills/muertes: casi siempre reflejan el resultado
# en vez de explicarlo, así que por defecto se excluyen del modelo del dashboard.
OUTCOME_LIKE = frozenset({"log_kda", "kill_participation"})


# ---------- Datos cacheados ----------
@st.cache_data(show_spinner=False)
def load_matchups(puuid: str) -> list[dict]:
    """Streamlit vuelve a ejecutar todo el script con cada interacción: esto evita recalcular."""
    return get_my_matchups(puuid)


@st.cache_data(ttl=600, show_spinner=False)
def load_analysis_data() -> pd.DataFrame:
    """Los 10 jugadores de cada partida guardada, con las métricas de analysis.py."""
    return build_features(load_data())


@st.cache_data(ttl=600, show_spinner="Entrenando el modelo...")
def fit_role_cached(role: str, features: tuple[str, ...]) -> dict | None:
    """Modelo 'vs rival' de un rol. `features` es tuple porque la llave del caché debe ser inmutable.

    No recibe el DataFrame como argumento: así Streamlit no tiene que calcular el hash
    de todos los datos en cada interacción, solo el de (role, features).
    """
    paired = vs_rival(load_analysis_data(), list(features))
    return fit_role(paired, role, list(features), paired=True)


# ---------- Consulta temporal ----------
# Los datos de una consulta temporal se guardan en st.session_state["live"], NO en
# st.cache_data: el caché es compartido por todos los que abren la app y dura hasta
# su TTL; session_state es de esta pestaña del navegador y se borra con ella.
def start_live(player) -> None:
    st.session_state["live"] = {
        "player": player,
        "matchups": get_my_matchups(player.puuid, player.db),
        "analysis": build_features(load_data(player.db)),
        "fits": {},  # modelos ya entrenados, por (rol, features)
    }


def release_live() -> None:
    """Libera la consulta temporal y olvida a ese jugador (su puuid, su Riot ID y el chat)."""
    live = st.session_state.pop("live", None)
    if live:
        release(live["player"])
        for key in ("puuid", "riot_id", "chat"):
            st.session_state.pop(key, None)


def fit_for(role: str, features: tuple[str, ...]) -> dict | None:
    live = st.session_state.get("live")
    if live is None:
        return fit_role_cached(role, features)
    if (role, features) not in live["fits"]:
        paired = vs_rival(live["analysis"], list(features))
        live["fits"][(role, features)] = fit_role(paired, role, list(features), paired=True)
    return live["fits"][(role, features)]


# ---------- Utilidades ----------
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
TEMPORARY = "Consulta temporal (no se guarda)"
mode = st.radio("Modo", ["Mi cuenta (se guarda en la base)", TEMPORARY], horizontal=True,
                help="La consulta temporal descarga las partidas, las analiza en memoria y las "
                     "borra al liberar, al buscar a otro jugador o al cerrar la pestaña.")
temporary = mode == TEMPORARY

riot_id = st.text_input("Riot ID", placeholder="Nombre#TAG")
col_a, col_b = st.columns(2)
if temporary:
    sync_first = False
    col_a.caption("Se descargan las últimas partidas de SoloQ. Con menos de 25 partidas "
                  "en un rol, el modelo de '¿Qué te hace ganar?' no alcanza a entrenarse.")
    count = col_b.number_input("Cuántas partidas descargar", 10, 100, 40, key="count_live")
else:
    sync_first = col_a.checkbox("Descargar partidas nuevas de Riot (solo baja las que no tienes)")
    count = col_b.number_input("Cuántas partidas revisar", 1, 100, 100, key="count_db")

if st.button("Analizar"):
    parsed = parse_riot_id(riot_id)
    if parsed is None:
        st.warning("Escribe tu Riot ID completo, con el tag: Nombre#TAG")
    else:
        game_name, tagline = parsed
        try:
            # Una búsqueda nueva siempre suelta la consulta temporal anterior.
            release_live()
            if temporary:
                bar = st.progress(0.0, text="Buscando jugador...")
                player = fetch_player_live(
                    game_name, tagline, count=int(count),
                    progress=lambda i, n: bar.progress(i / n, text=f"Descargando partida {i}/{n}"),
                )
                bar.empty()
                start_live(player)
                puuid = player.puuid
            elif sync_first:
                bar = st.progress(0.0, text="Buscando partidas...")
                puuid = sync_my_matches(
                    game_name, tagline, count=int(count),
                    progress=lambda i, n: bar.progress(i / n, text=f"Descargando partida {i}/{n}"),
                )
                bar.empty()
                # Hay partidas nuevas: el modelo debe reentrenarse con ellas.
                load_analysis_data.clear()
                fit_role_cached.clear()
                load_matchups.clear()
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
live = st.session_state.get("live")
# Toda la página lee de `db`: la base en disco, o la base en memoria de la consulta temporal.
db = live["player"].db if live else engine
matchups = live["matchups"] if live else load_matchups(puuid)

st.header(st.session_state["riot_id"])
if live:
    p = live["player"]
    info_col, button_col = st.columns([4, 1])
    info_col.info(
        f"Consulta temporal: {p.downloaded} partidas en memoria"
        f"{f' ({p.failed} no se pudieron descargar)' if p.failed else ''}. "
        "No se guardó nada en la base de datos."
    )
    if button_col.button("Liberar datos", type="primary", width="stretch"):
        release_live()
        st.rerun()

cols = st.columns(3)
for col, queue, label in [(cols[0], "RANKED_SOLO_5x5", "SoloQ"), (cols[1], "RANKED_FLEX_SR", "Flex")]:
    r = get_current_rank(puuid, queue, db)
    if r:
        col.metric(label, f"{r.tier} {r.rank_division}", f"{r.league_points} LP")
if matchups:
    cols[2].metric("Winrate (partidas analizadas)", f"{overall_winrate(matchups):.1%}", f"{len(matchups)} partidas")

champs = pd.DataFrame(calculate_champion_stats(matchups))
if champs.empty:
    st.warning("No hay partidas CLASSIC guardadas. Marca 'Descargar partidas'.")
    st.stop()

tab_champs, tab_trend, tab_mu, tab_win, tab_coach = st.tabs(
    ["Campeones", "Tendencia", "Matchups", "¿Qué te hace ganar?", "Coach"]
)

# ---------- Campeones ----------
with tab_champs:
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

# ---------- Tendencia ----------
with tab_trend:
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

# ---------- Matchups ----------
with tab_mu:
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

# ---------- ¿Qué te hace ganar? ----------
with tab_win:
    st.subheader("¿Qué te hace ganar?")
    st.caption(
        "El modelo compara a cada jugador contra su rival de línea en tus partidas y mide qué "
        "diferencias van **asociadas** con ganar. No demuestra causa: usa stats del final de la "
        "partida, y muchas suben justamente porque vas ganando."
    )

    data = live["analysis"] if live else load_analysis_data()
    mine = data[data["puuid"] == puuid]

    played = mine["team_position"].value_counts()
    default_role = played.index[0] if not played.empty and played.index[0] in ROLES else ROLES[0]

    c1, c2 = st.columns([1, 2])
    role = c1.selectbox(
        "Rol", ROLES, index=ROLES.index(default_role),
        format_func=lambda r: f"{ROLE_LABELS[r]} ({played.get(r, 0)} partidas tuyas)",
    )
    hide_outcome = c2.checkbox(
        "Excluir KDA y participación en kills (dependen del resultado y tapan al resto)",
        value=True,
    )

    features = features_for(role, RIVAL_FEATURES, drop_kda=False,
                            exclude=OUTCOME_LIKE if hide_outcome else frozenset())
    result = fit_for(role, tuple(features))

    if result is None:
        st.info(f"No hay suficientes partidas de {ROLE_LABELS[role]} para entrenar el modelo. "
                "Descarga más partidas.")
    else:
        m1, m2 = st.columns(2)
        m1.metric("Exactitud del modelo", f"{result['accuracy']:.0%}",
                  f"± {result['accuracy_std']:.0%}", delta_color="off",
                  help="% de partidas en las que el modelo acierta quién gana la línea, medido "
                       "en partidas que no vio al entrenar. Adivinar al azar da 50%.")
        m2.metric("Filas usadas", result["rows"], help="2 por partida: tú y tu rival, o los dos "
                                                       "jugadores de ese rol en tus partidas.")

        coefs = (
            pd.DataFrame({"metric": list(result["coefs"]), "coef": list(result["coefs"].values())})
            .assign(abs=lambda d: d["coef"].abs(),
                    label=lambda d: d["metric"].map(METRIC_LABELS),
                    efecto=lambda d: d["coef"].map(lambda c: "Más → ganar" if c > 0 else "Más → perder"))
            .sort_values("abs")  # en barras horizontales, lo último queda arriba
        )
        fig = px.bar(
            coefs, x="coef", y="label", orientation="h", color="efecto",
            color_discrete_map={"Más → ganar": "#2a9d8f", "Más → perder": "#e76f51"},
            labels={"coef": "Peso en el modelo (tener más que tu rival)", "label": "", "efecto": ""},
        )
        fig.add_vline(x=0, line_width=1, opacity=0.5)
        st.plotly_chart(fig, width="stretch")

        st.markdown(f"**Tú vs. los ganadores de {ROLE_LABELS[role]}**")
        mine_role = mine[mine["team_position"] == role]
        if mine_role.empty:
            st.info(f"No tienes partidas de {ROLE_LABELS[role]}: el modelo de arriba es de los "
                    "otros jugadores, pero no hay con qué compararte.")
        else:
            # Ganadores = los OTROS jugadores que ganaron en ese rol. Si te incluyera,
            # tus propias victorias empujarían el promedio hacia ti.
            winners = data[(data["team_position"] == role) & (data["win"] == 1) & (data["puuid"] != puuid)]
            table = pd.DataFrame({
                "Métrica": [METRIC_LABELS[f] for f in features],
                "Tú": [mine_role[f].mean() for f in features],
                "Ganadores del rol": [winners[f].mean() for f in features],
                "Peso en el modelo": [result["coefs"][f] for f in features],
            })
            table["Diferencia"] = table["Tú"] - table["Ganadores del rol"]
            table = table[["Métrica", "Tú", "Ganadores del rol", "Diferencia", "Peso en el modelo"]]
            table = table.reindex(table["Peso en el modelo"].abs().sort_values(ascending=False).index)
            st.dataframe(
                table, hide_index=True, width="stretch",
                column_config={c: st.column_config.NumberColumn(format="%.2f")
                               for c in ["Tú", "Ganadores del rol", "Diferencia", "Peso en el modelo"]},
            )
            st.caption(f"Tus {len(mine_role)} partidas de {ROLE_LABELS[role]} contra {len(winners)} "
                       "victorias de otros jugadores en ese rol, en las mismas partidas que tú.")

# ---------- Coach ----------
with tab_coach:
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
        context = build_prompt(game_name, tagline, puuid, matchups, db=db)

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
