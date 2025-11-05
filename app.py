# client_streamlit.py
import socket
import threading
import time
import queue

import streamlit as st

ENCODING = "utf-8"
DEFAULT_PORT = 8888


# ---------- low-level helpers ----------

def send_line(sock: socket.socket, text: str):
    """Send one logical line (terminated by '\\n') to the server."""
    try:
        sock.sendall((text + "\n").encode(ENCODING))
    except OSError:
        pass


def append_log(msg: str):
    """Push a message into the session log for display."""
    if "log" not in st.session_state:
        st.session_state.log = []
    st.session_state.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
    # Avoid infinite growth
    if len(st.session_state.log) > 200:
        st.session_state.log = st.session_state.log[-200:]


def update_scoreboard_from_payload(payload: str):
    """
    payload looks like:
        user1:3|user2:2|...
    or:
        EMPTY:0
    We'll store it as a list of (rank, username, pts).
    """
    if payload == "EMPTY:0":
        st.session_state.scoreboard = []
        return

    scoreboard_list = []
    rank = 1
    for chunk in payload.split("|"):
        if ":" in chunk:
            uname, pts = chunk.split(":", 1)
            scoreboard_list.append((rank, uname, pts))
            rank += 1
    st.session_state.scoreboard = scoreboard_list


# ---------- Listener thread (NO Streamlit calls here) ----------

def listener_thread(sock: socket.socket, my_username: str, ev_queue: "queue.Queue[tuple]"):
    """
    Background thread:
    - reads lines from TCP socket
    - pushes high-level events into ev_queue
    """
    buffer = ""
    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            ev_queue.put(("log", "[DISCONNECTED from server]"))
            break

        if not chunk:
            ev_queue.put(("log", "[SERVER CLOSED CONNECTION]"))
            break

        buffer += chunk.decode(ENCODING, errors="ignore")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue

            # ----- Questions -----
            if line.startswith("question:"):
                # Accept:
                #  - question:<id>:<text>
                #  - question:<id>:<stem>:<optA>:<optB>:<optC>:<optD>
                raw_parts = line.split(":")
                if len(raw_parts) < 3:
                    continue

                qid = raw_parts[1]

                if len(raw_parts) >= 7:
                    stem = raw_parts[2]
                    optA, optB, optC, optD = raw_parts[3:7]
                    q_data = {
                        "id": qid,
                        "stem": stem,
                        "options": [optA, optB, optC, optD],
                    }
                else:
                    # simple: question:<id>:<text with colons possible>
                    stem = ":".join(raw_parts[2:])
                    q_data = {
                        "id": qid,
                        "stem": stem,
                        "options": None,
                    }

                ev_queue.put(("question", q_data))
                ev_queue.put(("log", f"[QUESTION {qid}] {q_data['stem']}"))
                continue

            # ----- Broadcast messages -----
            if line.startswith("broadcast:"):
                msg = line.split(":", 1)[1]
                ev_queue.put(("log", f"[BROADCAST] {msg}"))

                if "TIMEUP" in msg and "Winner=" in msg:
                    if f"Winner={my_username}" in msg:
                        ev_queue.put(("feedback", "✅ You won this question!"))
                    elif "Winner=None" in msg:
                        ev_queue.put(("feedback", "⏰ Time is up. No correct answers."))
                    else:
                        ev_queue.put(("feedback", "⏰ Time is up. Someone else was first."))
                continue

            # ----- Scoreboard -----
            if line.startswith("score:"):
                payload = line.split(":", 1)[1]
                ev_queue.put(("score", payload))
                ev_queue.put(("log", "[SCOREBOARD UPDATED]"))
                continue

            # ----- Error codes -----
            if line.startswith("error:"):
                code = line.split(":", 1)[1]
                if code == "username_taken":
                    ev_queue.put((
                        "username_error",
                        "This username is already taken. Please choose another one.",
                    ))
                    ev_queue.put(("log", "[ERROR] Username already taken"))
                else:
                    ev_queue.put(("log", f"[ERROR] {code}"))
                continue

            # ----- Fallback -----
            ev_queue.put(("log", f"[SERVER MSG] {line}"))

    # On exit
    ev_queue.put(("disconnected", None))
    try:
        sock.close()
    except OSError:
        pass


# ---------- Process events in main Streamlit thread ----------

def process_events():
    """Pull all pending events from event_queue and apply them to session_state."""
    ev_queue: "queue.Queue[tuple]" = st.session_state.event_queue
    while True:
        try:
            kind, payload = ev_queue.get_nowait()
        except queue.Empty:
            break

        if kind == "log":
            append_log(payload)

        elif kind == "question":
            st.session_state.current_question = payload
            st.session_state.last_answer = None
            st.session_state.feedback = ""

        elif kind == "feedback":
            st.session_state.feedback = payload

        elif kind == "score":
            update_scoreboard_from_payload(payload)

        elif kind == "username_error":
            st.session_state.username_error = payload

        elif kind == "disconnected":
            st.session_state.connected = False
            st.session_state.sock = None
            st.session_state.listener_started = False  # allow reconnect


# ---------- Streamlit app ----------

st.set_page_config(
    page_title="Transport Layer Quiz (TCP)",
    page_icon="🧠",
    layout="wide",
)

# Initialize session state
if "sock" not in st.session_state:
    st.session_state.sock = None
if "connected" not in st.session_state:
    st.session_state.connected = False
if "current_question" not in st.session_state:
    st.session_state.current_question = None
if "scoreboard" not in st.session_state:
    st.session_state.scoreboard = []
if "log" not in st.session_state:
    st.session_state.log = []
if "last_answer" not in st.session_state:
    st.session_state.last_answer = None
if "feedback" not in st.session_state:
    st.session_state.feedback = ""
if "username_error" not in st.session_state:
    st.session_state.username_error = ""
if "my_username" not in st.session_state:
    st.session_state.my_username = ""
if "server_ip" not in st.session_state:
    st.session_state.server_ip = "192.168.55.66"
if "listener_started" not in st.session_state:
    st.session_state.listener_started = False
# IMPORTANT: persistent queue (not recreated every rerun)
if "event_queue" not in st.session_state:
    st.session_state.event_queue = queue.Queue()

# First thing: apply any incoming events from the socket
process_events()

st.title("🧠 Transport Layer Quiz (TCP) — Kahoot-style (TCP)")

# ----- LOBBY / CONNECTION -----
with st.sidebar:
    st.header("Lobby")

    server_ip = st.text_input("Server IP", value=st.session_state.server_ip)
    username = st.text_input("Username", value=st.session_state.my_username)

    connect_btn = st.button("Join Game")

    if connect_btn and not st.session_state.connected:
        st.session_state.username_error = ""
        if not server_ip.strip() or not username.strip():
            st.session_state.username_error = "Server IP and Username are required."
        else:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((server_ip.strip(), DEFAULT_PORT))
                st.session_state.sock = s
                st.session_state.server_ip = server_ip.strip()
                st.session_state.my_username = username.strip()
                st.session_state.connected = True
                append_log(
                    f"[CONNECTED to {server_ip.strip()}:{DEFAULT_PORT} as {username.strip()}]"
                )

                # send join:<username>
                send_line(s, f"join:{username.strip()}")
                append_log(f"[JOIN SENT as {username.strip()}]")

                # start listener thread (only once)
                if not st.session_state.listener_started:
                    t = threading.Thread(
                        target=listener_thread,
                        args=(s, username.strip(), st.session_state.event_queue),
                        daemon=True,
                    )
                    t.start()
                    st.session_state.listener_started = True

            except OSError as e:
                st.session_state.username_error = f"Connection error: {e}"
                append_log(f"[ERROR connecting: {e}]")
                st.session_state.connected = False
                st.session_state.sock = None

    if st.session_state.username_error:
        st.error(st.session_state.username_error)
    elif st.session_state.connected:
        st.success(f"Connected as {st.session_state.my_username}")
    else:
        st.info("Enter the server IP and your username, then click Join Game.")


# ----- MAIN LAYOUT -----
col_left, col_right = st.columns([2, 1])

# LEFT: Question + Answer
with col_left:
    st.subheader("Question")

    if st.session_state.connected and st.session_state.current_question:
        q = st.session_state.current_question
        stem = q["stem"]
        opts = q["options"]  # either [A,B,C,D] or None

        st.markdown(f"### Q{q['id']}: {stem}")

        labels = ["A", "B", "C", "D"]
        cols_btns = st.columns(2)

        for i, label in enumerate(labels):
            with cols_btns[i % 2]:
                if opts is not None and i < len(opts):
                    btn_label = f"{label}) {opts[i]}"
                else:
                    btn_label = f"{label}"
                btn = st.button(btn_label, key=f"btn_{label}", use_container_width=True)
                if btn and st.session_state.sock:
                    send_line(st.session_state.sock, f"answer:{label}")
                    st.session_state.last_answer = label
                    append_log(f"[ANSWER SENT '{label}']")

        if st.session_state.last_answer:
            st.info(f"Your last answer: {st.session_state.last_answer}")
    elif st.session_state.connected:
        st.info("Waiting for the next question from the server...")
    else:
        st.info("Join the game from the sidebar to start playing.")

    if st.session_state.feedback:
        st.markdown("---")
        st.subheader("Feedback")
        st.write(st.session_state.feedback)

# RIGHT: Leaderboard + Live log
with col_right:
    st.subheader("Leaderboard")
    if st.session_state.scoreboard:
        for rank, uname, pts in st.session_state.scoreboard:
            st.write(f"**{rank}. {uname}** — {pts} pts")
    else:
        st.write("_No scores yet_")

    st.markdown("---")
    st.subheader("Live Feed")
    for line in st.session_state.log[-20:]:
        st.code(line)


# ---------- AUTO-REFRESH WHILE CONNECTED ----------
if st.session_state.connected:
    time.sleep(0.2)  # don't hammer CPU
    if hasattr(st, "rerun"):           # Newer Streamlit
        st.rerun()
    else:                              # Older Streamlit
        st.experimental_rerun()
