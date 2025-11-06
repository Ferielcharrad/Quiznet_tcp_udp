"""
Streamlit TCP client for a Kahoot-style Transport Layer Quiz.

One student runs the TCP server; other players use this app to:
- Connect to the server.
- Receive and display quiz questions.
- Send answers (A/B/C/D).
- View live leaderboard and log messages.

The networking and parsing work in a background listener thread, which
pushes events into a queue. The Streamlit app processes this queue on
each rerun and updates session state accordingly.
"""

import queue
import re
import socket
import threading
import time

import streamlit as st

ENCODING = "utf-8"
DEFAULT_PORT = 8888


# ---------- low-level helpers ----------


def send_line(sock: socket.socket, text: str) -> None:
    """
    Send one logical line to the server.

    The line is terminated with a newline so that the server can use
    line-based parsing (read until '\n').
    """
    try:
        sock.sendall((text + "\n").encode(ENCODING))
    except OSError:
        # Socket already closed or broken; ignore in client UI.
        pass


def append_log(msg: str) -> None:
    """
    Append a timestamped log message to the session-level log buffer.

    The log is used for the "Live Feed" in the UI and is capped to the
    last 200 entries to avoid unbounded growth.
    """
    if "log" not in st.session_state:
        st.session_state.log = []

    timestamp = time.strftime("%H:%M:%S")
    st.session_state.log.append(f"{timestamp}  {msg}")

    # Keep only the most recent 200 lines
    if len(st.session_state.log) > 200:
        st.session_state.log = st.session_state.log[-200:]


def update_scoreboard_from_payload(payload: str) -> None:
    """
    Update the scoreboard from a payload string received from the server.

    Payload formats:
        "user1:3|user2:2|..."
        or
        "EMPTY:0"

    The parsed scoreboard is stored as:
        [(rank, username, points), ...]
    in st.session_state.scoreboard.
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


def listener_thread(
    sock: socket.socket,
    my_username: str,
    ev_queue: "queue.Queue[tuple]",
) -> None:
    """
    Background thread that receives raw lines from the server and
    pushes high-level events into a thread-safe queue.

    This thread:
    - Reads bytes from the TCP socket.
    - Splits messages by newline.
    - Interprets message types: question, broadcast, score, error, etc.
    - Converts them into events (kind, payload) and puts them into
      ev_queue.

    Streamlit is never called directly here because Streamlit is not
    thread-safe. Only the main thread reads ev_queue and updates the UI.
    """
    buffer = ""

    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            # Socket error => treat as disconnection.
            ev_queue.put(("log", "[DISCONNECTED from server]"))
            break

        if not chunk:
            # recv() returned empty => server closed connection.
            ev_queue.put(("log", "[SERVER CLOSED CONNECTION]"))
            break

        buffer += chunk.decode(ENCODING, errors="ignore")

        # Process complete lines one by one
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
                    opt_a, opt_b, opt_c, opt_d = raw_parts[3:7]
                    q_data = {
                        "id": qid,
                        "stem": stem,
                        "options": [opt_a, opt_b, opt_c, opt_d],
                    }
                else:
                    # Simple format: question:<id>:<text with colons possible>
                    stem = ":".join(raw_parts[2:])
                    q_data = {
                        "id": qid,
                        "stem": stem,
                        "options": None,
                    }

                # Notify UI about the new question
                ev_queue.put(("question", q_data))
                ev_queue.put(("log", f"[QUESTION {qid}] {q_data['stem']}"))
                continue

            # ----- Broadcast messages -----
            if line.startswith("broadcast:"):
                msg = line.split(":", 1)[1]
                ev_queue.put(("log", f"[BROADCAST] {msg}"))

                # Special case: TIMEUP messages that also declare a winner
                if "TIMEUP" in msg and "Winner=" in msg:
                    if f"Winner={my_username}" in msg:
                        ev_queue.put(("feedback", "✅ You won this question!"))
                    elif "Winner=None" in msg:
                        ev_queue.put(
                            (
                                "feedback",
                                "⏰ Time is up. No correct answers.",
                            )
                        )
                    else:
                        ev_queue.put(
                            (
                                "feedback",
                                "⏰ Time is up. Someone else was first.",
                            )
                        )
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
                    ev_queue.put(
                        (
                            "username_error",
                            "This username is already taken. "
                            "Please choose another one.",
                        )
                    )
                    ev_queue.put(("log", "[ERROR] Username already taken"))
                elif code == "lobby_full":
                    ev_queue.put(
                        (
                            "username_error",
                            "The game already has the maximum number of "
                            "players. Try again later.",
                        )
                    )
                    ev_queue.put(("log", "[ERROR] Lobby full"))
                else:
                    ev_queue.put(("log", f"[ERROR] {code}"))
                continue

            # ----- Fallback: unknown server message -----
            ev_queue.put(("log", f"[SERVER MSG] {line}"))

    # Thread end: notify main app and close socket
    ev_queue.put(("disconnected", None))
    try:
        sock.close()
    except OSError:
        pass


# ---------- Process events (link thread → UI) ----------


def process_events() -> None:
    """
    Drain the event queue and update Streamlit session state.

    This function is called once per Streamlit run. It:
    - Reads all available (kind, payload) events.
    - Updates the appropriate fields in st.session_state.
    - Drives the UI (question display, feedback, scoreboard, etc.).

    This is the core bridge between the background listener_thread and
    the Streamlit interface.
    """
    ev_queue: "queue.Queue[tuple]" = st.session_state.event_queue

    while True:
        try:
            kind, payload = ev_queue.get_nowait()
        except queue.Empty:
            break

        if kind == "log":
            append_log(payload)
        elif kind == "question":
            # New active question
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
            # Reset connection-related state when server disconnects
            st.session_state.connected = False
            st.session_state.sock = None
            st.session_state.listener_started = False


# ---------- Streamlit app (main control flow) ----------


# Configure the page layout and title
st.set_page_config(
    page_title="Transport Layer Quiz (TCP)",
    page_icon="🧠",
    layout="wide",
)

# --------- Session state initialisation ---------
# This section guarantees that all required keys exist in session_state
# before we start rendering the UI.

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
    # Example default; change this to the server machine IP when needed
    st.session_state.server_ip = "192.168.53.115"   #IP Nour
    st.session_state.server_ip = "172.18.80.1" #IP Ines
if "listener_started" not in st.session_state:
    st.session_state.listener_started = False
if "event_queue" not in st.session_state:
    st.session_state.event_queue = queue.Queue()

# Process any pending events from the listener thread
process_events()

# Top-level title
st.title("🧠 Transport Layer Quiz (TCP) — Kahoot-style (TCP)")

# ===================== LOBBY (SIDEBAR) =====================
# Control flow:
# 1) User enters server IP + username.
# 2) On "Join Game", we create a TCP socket and connect.
# 3) We send "join:<username>" to the server.
# 4) We start listener_thread exactly once per connection.
# 5) Errors are shown as Streamlit messages.

with st.sidebar:
    st.header("Lobby")

    server_ip = st.text_input("Server IP", value=st.session_state.server_ip)
    username = st.text_input("Username", value=st.session_state.my_username)

    connect_btn = st.button("Join Game")

    if connect_btn and not st.session_state.connected:
        # Clear previous error
        st.session_state.username_error = ""

        if not server_ip.strip() or not username.strip():
            # Basic local validation
            st.session_state.username_error = (
                "Server IP and Username are required."
            )
        else:
            try:
                # Create and connect TCP socket
                sock_obj = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock_obj.connect((server_ip.strip(), DEFAULT_PORT))

                # Save connection info in session state
                st.session_state.sock = sock_obj
                st.session_state.server_ip = server_ip.strip()
                st.session_state.my_username = username.strip()
                st.session_state.connected = True

                append_log(
                    "[CONNECTED to "
                    f"{server_ip.strip()}:{DEFAULT_PORT} "
                    f"as {username.strip()}]"
                )

                # Notify server about our username
                send_line(sock_obj, f"join:{username.strip()}")
                append_log(f"[JOIN SENT as {username.strip()}]")

                # Start background listener thread if not already running
                if not st.session_state.listener_started:
                    thread = threading.Thread(
                        target=listener_thread,
                        args=(
                            sock_obj,
                            username.strip(),
                            st.session_state.event_queue,
                        ),
                        daemon=True,
                    )
                    thread.start()
                    st.session_state.listener_started = True

            except OSError as exc:
                # Connection error: display to user and reset state
                st.session_state.username_error = f"Connection error: {exc}"
                append_log(f"[ERROR connecting: {exc}]")
                st.session_state.connected = False
                st.session_state.sock = None

    # Feedback messages in sidebar
    if st.session_state.username_error:
        st.error(st.session_state.username_error)
    elif st.session_state.connected:
        st.success(f"Connected as {st.session_state.my_username}")
    else:
        st.info(
            "Enter the server IP and your username, then click Join Game."
        )

# ===================== MAIN LAYOUT =====================
# Left column: current question + answer buttons.
# Right column: leaderboard + live log.

col_left, col_right = st.columns([2, 1])

with col_left:
    st.subheader("Question")

    if st.session_state.connected and st.session_state.current_question:
        question = st.session_state.current_question
        stem = question["stem"]
        opts = question["options"]  # either [optA, optB, ...] or None

        st.markdown(f"### Q{question['id']}: {stem}")

        # Decide which option letters (A/B/C/D) to show:

        if opts is not None:
            # Structured options provided by server: [A, B, C, D...]
            all_labels = ["A", "B", "C", "D"]
            labels = all_labels[: len(opts)]
        else:
            # No structured options; try to auto-detect "A) B) C) D)" in text
            found = sorted(set(re.findall(r"\b([A-D])\)", stem)))
            if found:
                labels = found
            else:
                # Fallback: always show A-D if nothing detected
                labels = ["A", "B", "C", "D"]

        # Show answer buttons in 2 columns (A/B on left, C/D on right)
        cols_btns = st.columns(2)

        for i, label in enumerate(labels):
            with cols_btns[i % 2]:
                if opts is not None and i < len(opts):
                    btn_label = f"{label}) {opts[i]}"
                else:
                    btn_label = f"{label}"

                # When a button is clicked:
                # - Send "answer:<letter>" to server.
                # - Save last answer in session state.
                btn = st.button(
                    btn_label,
                    key=f"btn_{label}",
                    use_container_width=True,
                )
                if btn and st.session_state.sock:
                    send_line(st.session_state.sock, f"answer:{label}")
                    st.session_state.last_answer = label
                    append_log(f"[ANSWER SENT '{label}']")

        # Show which option the player clicked last
        if st.session_state.last_answer:
            st.info(f"Your last answer: {st.session_state.last_answer}")

    elif st.session_state.connected:
        # Connected but no active question yet
        st.info("Waiting for the next question from the server...")
    else:
        # Not connected at all
        st.info("Join the game from the sidebar to start playing.")

    # Feedback from server after each question (win/lose/time up)
    if st.session_state.feedback:
        st.markdown("---")
        st.subheader("Feedback")
        st.write(st.session_state.feedback)

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

# ===================== AUTO-RERUN LOOP =====================
# As long as we are connected, we:
# - Sleep briefly to avoid hammering the CPU.
# - Force a rerun so that new events from listener_thread
#   (placed in ev_queue) can be processed and reflected in the UI.

if st.session_state.connected:
    time.sleep(0.2)
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()
