"""
🎮 KAHOOT-STYLE TCP Quiz Client
Enhanced with modern UI, time-based scoring, and engaging user experience!

Features:
- Vibrant Kahoot-inspired color scheme
- Time-based bonus scoring system
- Animated feedback and countdowns
- Professional leaderboard with podium
- Streak tracking and achievements
- Waiting room with player count
"""

import queue
import re
import socket
import threading
import time
from datetime import datetime

import streamlit as st

ENCODING = "utf-8"
DEFAULT_PORT = 8888

# 🎨 Kahoot-inspired color palette
KAHOOT_COLORS = {
    "A": "#E21B3C",  # Red
    "B": "#1368CE",  # Blue
    "C": "#D89E00",  # Yellow/Gold
    "D": "#26890C",  # Green
}


# ---------- Helper Functions ----------

def parse_question_text_and_options(stem: str):
    """Parse question stem of the form 'Question text A) OptA B) OptB [C) .. D) ..]'.

    Returns:
    - question_text: the stem without inline options
    - options_map: dict like { 'A': 'OptA', 'B': 'OptB', ... } in order found
    - labels_order: list of labels in natural order e.g. ['A','B','C','D']
    """
    if not isinstance(stem, str):
        return stem, {}, []

    # Identify where options start (first 'A)') to separate question text
    m = re.search(r"\bA\)", stem)
    question_text = stem[: m.start()].strip() if m else stem.strip()

    # Capture options like 'A) text  B) text  C) text  D) text'
    # Non-greedy match until next label or end
    pattern = r"([A-D])\)\s*(.*?)(?=\s*[A-D]\)\s*|$)"
    options_map = {}
    labels_order = []
    for label, text in re.findall(pattern, stem, flags=re.S):
        clean_text = " ".join(text.strip().split())  # collapse whitespace
        if clean_text:
            options_map[label] = clean_text
            labels_order.append(label)

    return question_text, options_map, labels_order

def send_line(sock: socket.socket, text: str) -> None:
    """Send one logical line to the server."""
    try:
        sock.sendall((text + "\n").encode(ENCODING))
    except OSError:
        pass


def append_log(msg: str) -> None:
    """Append a timestamped log message to the session-level log buffer."""
    if "log" not in st.session_state:
        st.session_state.log = []

    timestamp = time.strftime("%H:%M:%S")
    st.session_state.log.append(f"{timestamp}  {msg}")

    # Keep only the most recent 200 lines
    if len(st.session_state.log) > 200:
        st.session_state.log = st.session_state.log[-200:]


def update_scoreboard_from_payload(payload: str) -> None:
    """Update the scoreboard from a payload string received from the server."""
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


# ---------- Listener Thread ----------

def listener_thread(
    sock: socket.socket,
    my_username: str,
    ev_queue: "queue.Queue[tuple]",
) -> None:
    """Background thread that receives messages from the server."""
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
                continue            # ----- Questions -----
            if line.startswith("question:"):
                raw_parts = line.split(":")
                if len(raw_parts) < 4:  # Now needs at least qid, timeout, and stem
                    continue

                qid = raw_parts[1]
                try:
                    timeout = int(raw_parts[2])
                except (ValueError, IndexError):
                    continue  # Skip if timeout is not a valid integer

                stem = ":".join(raw_parts[3:])
                q_data = {
                    "id": qid,
                    "stem": stem,
                    "timeout": timeout,
                    "options": None,  # Options are parsed from the stem later
                }

                ev_queue.put(("question", q_data))
                ev_queue.put(("page", "question"))  # Force show question page
                ev_queue.put(("clear_feedback", None))  # Clear old feedback
                ev_queue.put(("log", f"[QUESTION {qid}] {q_data['stem']}"))
                continue            # ----- Page transitions -----
            if line.startswith("show:"):
                page_name = line.split(":", 1)[1]
                # Ignore all show: commands - we control page flow based on question/score events
                ev_queue.put(("log", f"[PAGE] Server requested {page_name} (ignored)"))
                continue
            
            # ----- Timer updates -----
            if line.startswith("timer:"):
                remaining = line.split(":", 1)[1]
                ev_queue.put(("timer", int(remaining)))
                continue

            # ----- Individual feedback (new format) -----
            if line.startswith("feedback:"):
                # Format: feedback:{username}:correct:{points}:{time}
                # or: feedback:{username}:wrong:0:0
                # or: feedback:{username}:timeout:0:0
                parts = line.split(":")
                if len(parts) >= 5:
                    username = parts[1]
                    result = parts[2]  # correct/wrong/timeout
                    points = parts[3]
                    time_taken = parts[4]
                    
                    if username == my_username:
                        if result == "correct":
                            ev_queue.put(("feedback", f"✅ CORRECT! +{points} pts ({time_taken}s)"))
                            ev_queue.put(("correct", True))
                            ev_queue.put(("points", int(points)))
                        elif result == "wrong":
                            ev_queue.put(("feedback", "❌ Wrong answer"))
                            ev_queue.put(("correct", False))
                        elif result == "timeout":
                            ev_queue.put(("feedback", "⏰ Time's up!"))
                            ev_queue.put(("correct", False))
                continue            # ----- Broadcast messages -----
            if line.startswith("broadcast:"):
                msg = line.split(":", 1)[1]
                ev_queue.put(("log", f"[BROADCAST] {msg}"))

                # Handle TIMEUP broadcast for showing winner
                if "TIMEUP" in msg and "Winner=" in msg:
                    if f"Winner={my_username}" in msg:
                        ev_queue.put(("result_message", f"🏆 You were the fastest!"))
                    elif "Winner=None" in msg:
                        ev_queue.put(("result_message", "No one got it right"))
                    else:
                        # Extract winner name
                        match = re.search(r"Winner=(\w+)", msg)
                        if match:
                            winner = match.group(1)
                            ev_queue.put(("result_message", f"🥇 {winner} was fastest!"))
                continue

            # ----- Scoreboard -----
            if line.startswith("score:"):
                payload = line.split(":", 1)[1]
                ev_queue.put(("score", payload))
                # Switch to results page ONLY after receiving scoreboard AND after user answered
                # This ensures we show results after the question is complete
                ev_queue.put(("show_results_now", None))
                ev_queue.put(("log", "[SCOREBOARD UPDATED]"))
                continue

            # ----- Error codes -----
            if line.startswith("error:"):
                code = line.split(":", 1)[1]
                if code == "username_taken":
                    ev_queue.put(("username_error", "This username is already taken. Please choose another one."))
                    ev_queue.put(("log", "[ERROR] Username already taken"))
                elif code == "ip_exists":
                    ev_queue.put(("username_error", "This machine is already connected. Please use another device."))
                    ev_queue.put(("log", "[ERROR] IP already connected"))
                elif code == "lobby_full":
                    ev_queue.put(("username_error", "The game already has the maximum number of players. Try again later."))
                    ev_queue.put(("log", "[ERROR] Lobby full"))
                else:
                    ev_queue.put(("log", f"[ERROR] {code}"))
                continue

            ev_queue.put(("log", f"[SERVER MSG] {line}"))

    ev_queue.put(("disconnected", None))
    try:
        sock.close()
    except OSError:
        pass


# ---------- Process Events ----------

def process_events() -> None:
    """Drain the event queue and update Streamlit session state."""
    ev_queue: "queue.Queue[tuple]" = st.session_state.event_queue
    
    while True:
        try:
            kind, payload = ev_queue.get_nowait()
        except queue.Empty:
            break

        if kind == "log":
            append_log(payload)
        elif kind == "question":
            # When a new question arrives, force the page to "question" and clear everything
            st.session_state.current_question = payload
            st.session_state.last_answer = None
            st.session_state.feedback = ""
            st.session_state.result_message = ""
            st.session_state.question_start_time = time.time()
            st.session_state.current_page = "question"  # FORCE page to question
            # Set timer to the specific timeout for this question
            st.session_state.time_remaining = payload.get("timeout", 15)
            st.session_state.question_timeout = payload.get("timeout", 15)
        elif kind == "timer":
            st.session_state.time_remaining = payload
        elif kind == "page":
            # Only allow page transitions to "question" - ignore all others
            if payload == "question":
                st.session_state.current_page = payload
        elif kind == "clear_feedback":
            # Clear feedback when new question starts
            st.session_state.feedback = ""
            st.session_state.result_message = ""
            st.session_state.last_points_earned = 0
        elif kind == "show_results_now":
            # Show results if we're on the question page and have a current question
            # (User may or may not have answered - could be timeout/skip)
            if (st.session_state.current_page == "question" and
                st.session_state.current_question is not None):
                st.session_state.current_page = "results"
        elif kind == "feedback":
            # Store feedback but DON'T change page - wait for score update
            st.session_state.feedback = payload
        elif kind == "result_message":
            # Store result message but DON'T change page - wait for score update
            st.session_state.result_message = payload
        elif kind == "points":
            st.session_state.last_points_earned = payload
        elif kind == "correct":
            if payload:  # True = correct answer
                st.session_state.total_correct += 1
                st.session_state.answer_streak += 1
            else:  # False = wrong answer
                st.session_state.answer_streak = 0
            st.session_state.total_answered += 1
        elif kind == "score":
            # Update scoreboard data but don't trigger page change here
            # The "show_results_now" event will handle page transition
            update_scoreboard_from_payload(payload)
        elif kind == "username_error":
            st.session_state.username_error = payload
        elif kind == "disconnected":
            # Before disconnecting, show final leaderboard if we have scores
            if st.session_state.scoreboard:
                st.session_state.current_page = "final_results"
                st.session_state.show_disconnect_message = True
            else:
                st.session_state.connected = False
                st.session_state.sock = None
                st.session_state.listener_started = False


# ---------- Streamlit App ----------

st.set_page_config(
    page_title="QuizNet",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for a premium Kahoot-style UI
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;900&display=swap');

    body {
        font-family: 'Montserrat', sans-serif;
    }

    .stApp {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
    }

    /* Hide Streamlit branding but keep sidebar toggle */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    
    /* Make sure sidebar is visible */
    [data-testid="stSidebar"] {
        display: block !important;
    }
    
    /* Ensure sidebar toggle button is visible */
    [data-testid="collapsedControl"] {
        display: block !important;
        visibility: visible !important;
    }

    /* General button styling */
    .stButton>button {
        border-radius: 12px !important;
        border: none !important;
        padding: 28px 24px !important;
        color: white !important;
        font-weight: 700 !important;
        font-family: 'Montserrat', sans-serif;
        font-size: 20px !important;
        transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;
        box-shadow: 0 8px 0 rgba(0,0,0,0.25), 0 4px 20px rgba(0,0,0,0.15);
        margin-bottom: 12px;
        height: auto !important;
        min-height: 80px;
        cursor: pointer;
        position: relative;
        overflow: hidden;
    }

    .stButton>button::before {
        content: '';
        position: absolute;
        top: 50%;
        left: 50%;
        width: 0;
        height: 0;
        border-radius: 50%;
        background: rgba(255,255,255,0.2);
        transform: translate(-50%, -50%);
        transition: width 0.6s, height 0.6s;
    }

    .stButton>button:hover::before {
        width: 300px;
        height: 300px;
    }

    .stButton>button:hover {
        transform: translateY(-4px) scale(1.02);
        box-shadow: 0 12px 0 rgba(0,0,0,0.25), 0 8px 30px rgba(0,0,0,0.2);
    }
    
    .stButton>button:active {
        transform: translateY(4px) scale(0.98);
        box-shadow: 0 4px 0 rgba(0,0,0,0.25), 0 2px 10px rgba(0,0,0,0.15);
    }

    /* Kahoot Answer Buttons - Colors with gradients */
    div[data-testid="stHorizontalBlock"]:nth-of-type(1)>div:nth-child(1) .stButton>button { 
        background: linear-gradient(135deg, #E21B3C 0%, #C41230 100%);
    }
    div[data-testid="stHorizontalBlock"]:nth-of-type(1)>div:nth-child(2) .stButton>button { 
        background: linear-gradient(135deg, #1368CE 0%, #0D4FA3 100%);
    }
    div[data-testid="stHorizontalBlock"]:nth-of-type(2)>div:nth-child(1) .stButton>button { 
        background: linear-gradient(135deg, #FFA500 0%, #D89E00 100%);
    }
    div[data-testid="stHorizontalBlock"]:nth-of-type(2)>div:nth-child(2) .stButton>button { 
        background: linear-gradient(135deg, #26890C 0%, #1D6909 100%);
    }

    .question-card {
        background: white;
        border-radius: 24px;
        padding: 50px;
        box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        margin: 30px 0;
        text-align: center;
        animation: slideInUp 0.6s cubic-bezier(0.4, 0, 0.2, 1);
        color: #1a202c;
        position: relative;
        overflow: hidden;
    }

    .question-card::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        height: 6px;
        background: linear-gradient(90deg, #E21B3C, #1368CE, #FFA500, #26890C);
    }
    
    @keyframes slideInUp {
        from { 
            opacity: 0; 
            transform: translateY(40px);
        }
        to { 
            opacity: 1; 
            transform: translateY(0);
        }
    }

    @keyframes fadeIn {
        from { opacity: 0; }
        to { opacity: 1; }
    }

    @keyframes scaleIn {
        from { 
            opacity: 0;
            transform: scale(0.8);
        }
        to { 
            opacity: 1;
            transform: scale(1);
        }
    }
    
    .leaderboard-item {
        background: rgba(255,255,255,0.95);
        border-radius: 16px;
        padding: 20px 30px;
        margin: 12px auto;
        box-shadow: 0 8px 20px rgba(0,0,0,0.15);
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        color: #1a202c;
        max-width: 700px;
        animation: fadeIn 0.5s ease-out;
    }
    
    .leaderboard-item:hover {
        transform: translateY(-4px) scale(1.02);
        box-shadow: 0 12px 30px rgba(0,0,0,0.2);
    }
    
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #1a202c 0%, #2d3748 100%);
    }
    
    @keyframes bounce {
        0%, 100% { transform: translateY(0); }
        50% { transform: translateY(-20px); }
    }

    @keyframes pulse {
        0%, 100% { transform: scale(1); }
        50% { transform: scale(1.05); }
    }

    /* Center align metrics */
    [data-testid="stMetric"] {
        text-align: center;
    }

    /* Timer styling */
    .timer-container {
        background: white;
        border-radius: 20px;
        padding: 24px;
        margin-bottom: 30px;
        box-shadow: 0 12px 30px rgba(0,0,0,0.2);
        color: #1a202c;
        animation: scaleIn 0.4s ease-out;
    }

    /* Result feedback animation */
    .result-feedback {
        animation: pulse 0.6s ease-in-out;
    }

    /* Waiting room animation */
    @keyframes spin {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }

    .loader {
        border: 5px solid rgba(255,255,255,0.3);
        border-top: 5px solid white;
        border-radius: 50%;
        width: 60px;
        height: 60px;
        animation: spin 1s linear infinite;
        margin: 30px auto;
    }
    </style>
""", unsafe_allow_html=True)

# Session state initialization
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
    st.session_state.server_ip = "192.168.1.28"
if "listener_started" not in st.session_state:
    st.session_state.listener_started = False
if "event_queue" not in st.session_state:
    st.session_state.event_queue = queue.Queue()
if "answer_streak" not in st.session_state:
    st.session_state.answer_streak = 0
if "total_correct" not in st.session_state:
    st.session_state.total_correct = 0
if "total_answered" not in st.session_state:
    st.session_state.total_answered = 0
if "question_start_time" not in st.session_state:
    st.session_state.question_start_time = None
if "current_page" not in st.session_state:
    st.session_state.current_page = "waiting"  # waiting, question, results, leaderboard
if "result_message" not in st.session_state:
    st.session_state.result_message = ""
if "last_points_earned" not in st.session_state:
    st.session_state.last_points_earned = 0
if "time_remaining" not in st.session_state:
    st.session_state.time_remaining = 15
if "question_timeout" not in st.session_state:
    st.session_state.question_timeout = 15
if "show_disconnect_message" not in st.session_state:
    st.session_state.show_disconnect_message = False

process_events()

# 🎮 Header
st.markdown("""
    <div style='text-align: center; padding-bottom: 30px;'>
        <h1 style='font-family: "Montserrat", sans-serif; font-weight: 900; font-size: 56px; margin: 0; text-shadow: 4px 4px 0px rgba(0,0,0,0.2);'>
            QuizNet
        </h1>
        <p style='font-size: 20px; margin-top: 10px; opacity: 0.8;'>
            The Transport Layer Challenge
        </p>
    </div>
""", unsafe_allow_html=True)

# ===================== SIDEBAR =====================
with st.sidebar:
    st.markdown("<h1 style='font-family: \"Montserrat\", sans-serif; font-weight: 900; text-align: center;'>Lobby</h1>", unsafe_allow_html=True)
    
    if st.session_state.connected:
        st.markdown(f"""
            <div style='background: #26890C; color: white; padding: 15px; border-radius: 8px; text-align: center; margin-bottom: 20px;'>
                <h3 style='margin: 0; font-weight: 700;'>Connected</h3>
                <p style='margin: 5px 0 0 0;'>Playing as <b>{st.session_state.my_username}</b></p>
            </div>
        """, unsafe_allow_html=True)
        
        st.markdown("### Your Stats")
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Correct", st.session_state.total_correct)
            st.metric("Streak", st.session_state.answer_streak)
        with col2:
            accuracy = (st.session_state.total_correct / st.session_state.total_answered * 100) if st.session_state.total_answered > 0 else 0
            st.metric("Accuracy", f"{accuracy:.0f}%")
            st.metric("Answered", st.session_state.total_answered)
    else:
        st.markdown("""
            <div style='background: #6366f1; color: white; padding: 15px; border-radius: 8px; text-align: center; margin-bottom: 20px;'>
                <h3 style='margin: 0; font-weight: 700;'>Join the Game!</h3>
            </div>
        """, unsafe_allow_html=True)

    server_ip = st.text_input("Server IP", value=st.session_state.server_ip, disabled=st.session_state.connected)
    username = st.text_input("Username", value=st.session_state.my_username, disabled=st.session_state.connected, max_chars=20)

    if not st.session_state.connected:
        connect_btn = st.button("Join Game", type="primary", use_container_width=True)
        
        if connect_btn:
            st.session_state.username_error = ""
            
            if not server_ip.strip() or not username.strip():
                st.session_state.username_error = "Server IP and Username are required."
            else:
                try:
                    sock_obj = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock_obj.connect((server_ip.strip(), DEFAULT_PORT))
                    st.session_state.sock = sock_obj
                    st.session_state.server_ip = server_ip.strip()
                    st.session_state.my_username = username.strip()
                    st.session_state.connected = True
                    
                    append_log(f"[CONNECTED to {server_ip.strip()}:{DEFAULT_PORT} as {username.strip()}]")
                    send_line(sock_obj, f"join:{username.strip()}")
                    append_log(f"[JOIN SENT as {username.strip()}]")
                    
                    if not st.session_state.listener_started:
                        thread = threading.Thread(
                            target=listener_thread,
                            args=(sock_obj, username.strip(), st.session_state.event_queue),
                            daemon=True,
                        )
                        thread.start()
                        st.session_state.listener_started = True
                        
                except OSError as exc:
                    st.session_state.username_error = f"Connection error: {exc}"
                    append_log(f"[ERROR connecting: {exc}]")
                    st.session_state.connected = False
                    st.session_state.sock = None
    else:
        # Disconnect button when connected
        st.markdown("---")
        if st.button("Disconnect", type="secondary", use_container_width=True):
            # Show final results page instead of immediate disconnect
            if st.session_state.scoreboard:
                st.session_state.current_page = "final_results"
                st.session_state.show_disconnect_message = True
                if st.session_state.sock:
                    try:
                        st.session_state.sock.close()
                    except:
                        pass
                st.session_state.sock = None
                st.session_state.listener_started = False
            else:
                # No scores yet, disconnect immediately
                if st.session_state.sock:
                    try:
                        st.session_state.sock.close()
                    except:
                        pass
                st.session_state.connected = False
                st.session_state.sock = None
                st.session_state.listener_started = False
                st.success("Disconnected from server")
                time.sleep(1)
            
            if hasattr(st, "rerun"):
                st.rerun()
            else:
                st.experimental_rerun()

    if st.session_state.username_error:
        st.error(st.session_state.username_error)

# ===================== MAIN LAYOUT =====================
col_main = st.container()

with col_main:
    # MULTI-PAGE FLOW: Question → Results → Leaderboard
      # PAGE 1: QUESTION PAGE
    if st.session_state.connected and st.session_state.current_page == "question" and st.session_state.current_question:
        # Force default background during question phase
        st.markdown("""
            <style>
                .stApp {
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                }
            </style>
        """, unsafe_allow_html=True)
        
        question = st.session_state.current_question
        stem = question["stem"]
        opts = question["options"]

        # Extract option texts from the stem if they are embedded (e.g., "A) TCP B) UDP ...")
        cleaned_stem, options_map, labels_in_stem = parse_question_text_and_options(stem)
        
        # Timer Display with premium styling
        time_remaining = st.session_state.time_remaining
        question_timeout = st.session_state.question_timeout
        
        # Dynamic color based on time
        if time_remaining > (question_timeout * 0.66):
            timer_color = "#10b981"
            timer_bg = "linear-gradient(135deg, #10b981, #059669)"
        elif time_remaining > (question_timeout * 0.33):
            timer_color = "#f59e0b"
            timer_bg = "linear-gradient(135deg, #f59e0b, #d97706)"
        else:
            timer_color = "#ef4444"
            timer_bg = "linear-gradient(135deg, #ef4444, #dc2626)"
        
        progress_percent = (time_remaining / question_timeout) * 100 if question_timeout > 0 else 0
        
        st.markdown(f"""
            <div class='timer-container'>
                <div style='display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;'>
                    <span style='font-size: 22px; font-weight: 700; color: #4a5568;'>Time Remaining</span>
                    <span style='font-size: 44px; font-weight: 900; color: {timer_color}; text-shadow: 0 2px 4px rgba(0,0,0,0.1);'>{time_remaining}s</span>
                </div>
                <div style='width: 100%; height: 16px; background: #e5e7eb; border-radius: 10px; overflow: hidden; box-shadow: inset 0 2px 4px rgba(0,0,0,0.1);'>
                    <div style='width: {progress_percent}%; height: 100%; background: {timer_bg}; 
                         transition: all 0.5s cubic-bezier(0.4, 0, 0.2, 1); border-radius: 10px;'></div>
                </div>
            </div>
        """, unsafe_allow_html=True)

        # Question Card with premium styling
        st.markdown(f"""
            <div class='question-card'>
                <div style='display: inline-block; background: linear-gradient(135deg, #667eea, #764ba2); 
                     color: white; padding: 8px 20px; border-radius: 20px; font-size: 16px; font-weight: 700; 
                     margin-bottom: 24px; box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);'>
                    Question {question['id']}
                </div>
                <h2 style='font-family: "Montserrat", sans-serif; font-weight: 700; font-size: 32px; 
                     line-height: 1.4; color: #1a202c; margin: 24px 0;'>{cleaned_stem}</h2>
            </div>
        """, unsafe_allow_html=True)

        # Determine options
        if opts is not None and isinstance(opts, (list, tuple)) and len(opts) > 0:
            all_labels = ["A", "B", "C", "D"]
            labels = all_labels[: len(opts)]
            # Build map from provided opts if server sent them separately
            options_map = {label: str(opts[i]) for i, label in enumerate(labels)}
        else:
            # Prefer labels detected in the stem via the parser
            labels = labels_in_stem if labels_in_stem else ["A", "B", "C", "D"]

        # Answer Buttons (2x2 grid) - only show if not answered
        if st.session_state.last_answer is None:
            # Create a list of labels and options
            options_with_labels = []
            # Build from parsed/provided options if available; otherwise show generic labels
            if options_map:
                for label in labels:
                    if label in options_map:
                        options_with_labels.append((label, options_map[label]))
            if not options_with_labels:
                for label in labels:
                    options_with_labels.append((label, f"Option {label}"))

            # Display buttons in a 2x2 grid
            for i in range(0, len(options_with_labels), 2):
                col1, col2 = st.columns(2)
                
                # Button 1
                with col1:
                    label, text = options_with_labels[i]
                    # Show the option text on the colored button; still send the label
                    if st.button(f"{text}", key=f"btn_{label}", use_container_width=True):
                        if st.session_state.sock and st.session_state.last_answer is None:
                            send_line(st.session_state.sock, f"answer:{label}")
                            st.session_state.last_answer = label
                            append_log(f"[ANSWER SENT '{label}']")
                            st.balloons()
                            if hasattr(st, "rerun"): st.rerun()
                            else: st.experimental_rerun()

                # Button 2 (if it exists)
                if i + 1 < len(options_with_labels):
                    with col2:
                        label, text = options_with_labels[i+1]
                        if st.button(f"{text}", key=f"btn_{label}", use_container_width=True):
                            if st.session_state.sock and st.session_state.last_answer is None:
                                send_line(st.session_state.sock, f"answer:{label}")
                                st.session_state.last_answer = label
                                append_log(f"[ANSWER SENT '{label}']")
                                st.balloons()
                                if hasattr(st, "rerun"): st.rerun()
                                else: st.experimental_rerun()
        else:
            # Show "answer locked in" message with premium styling
            st.markdown(f"""
                <div style='background: linear-gradient(135deg, rgba(16, 185, 129, 0.2), rgba(5, 150, 105, 0.2)); 
                     backdrop-filter: blur(10px);
                     color: white; padding: 28px; border-radius: 20px; text-align: center; margin-top: 30px;
                     border: 2px solid rgba(255,255,255,0.3); box-shadow: 0 8px 24px rgba(0,0,0,0.2);
                     animation: scaleIn 0.4s ease-out;'>
                    <div style='font-size: 48px; margin-bottom: 12px;'>✓</div>
                    <h2 style='margin: 0; font-weight: 800; font-size: 28px; text-shadow: 0 2px 4px rgba(0,0,0,0.2);'>
                        Answer Locked!
                    </h2>
                    <p style='margin: 12px 0 0 0; opacity: 0.95; font-size: 18px; font-weight: 600;'>
                        You selected: <span style='font-size: 24px; font-weight: 900;'>{st.session_state.last_answer}</span>
                    </p>
                    <div class='loader' style='margin-top: 20px;'></div>
                    <p style='margin: 8px 0 0 0; opacity: 0.9; font-size: 16px;'>Waiting for results...</p>
                </div>
            """, unsafe_allow_html=True)
    
    # PAGE 2: UNIFIED RESULTS & LEADERBOARD PAGE
    elif st.session_state.connected and st.session_state.current_page == "results":
        # Dynamic background based on result
        if st.session_state.feedback:
            if "CORRECT" in st.session_state.feedback:
                bg_gradient = "linear-gradient(135deg, #10b981 0%, #059669 100%)"
            elif "Wrong" in st.session_state.feedback:
                bg_gradient = "linear-gradient(135deg, #ef4444 0%, #dc2626 100%)"
            else:
                bg_gradient = "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)"
        else:
            bg_gradient = "linear-gradient(135deg, #667eea 0%, #764ba2 100%)"

        st.markdown(f"""
            <style>
                .stApp {{
                    background: {bg_gradient};
                    transition: background 0.8s cubic-bezier(0.4, 0, 0.2, 1);
                }}
            </style>
        """, unsafe_allow_html=True)

        # Results Container
        st.markdown("""
            <div style='text-align: center; padding-top: 2rem; animation: fadeIn 0.6s ease-out;'>
        """, unsafe_allow_html=True)

        # Feedback Section with premium animation
        if st.session_state.feedback:
            feedback_icon = "🎉" if "CORRECT" in st.session_state.feedback else ("💔" if "Wrong" in st.session_state.feedback else "⏰")
            
            st.markdown(f"""
                <div class='result-feedback' style='margin-bottom: 2rem;'>
                    <div style='font-size: 80px; animation: bounce 0.8s ease-in-out; margin-bottom: 1rem;'>{feedback_icon}</div>
                    <h1 style='font-size: 3.5rem; font-weight: 900; margin: 0; text-shadow: 0 6px 12px rgba(0,0,0,0.4); 
                         letter-spacing: 1px;'>{st.session_state.feedback}</h1>
                </div>
            """, unsafe_allow_html=True)
        
        # Winner/Result Message
        if st.session_state.result_message:
            st.markdown(f"""
                <div style='background: rgba(255,255,255,0.2); backdrop-filter: blur(10px); 
                     padding: 20px 40px; border-radius: 20px; display: inline-block; margin-bottom: 3rem;
                     box-shadow: 0 8px 24px rgba(0,0,0,0.2); animation: scaleIn 0.5s ease-out;'>
                    <p style='font-size: 1.5rem; font-weight: 700; margin: 0; text-shadow: 0 2px 4px rgba(0,0,0,0.2);'>
                        {st.session_state.result_message}
                    </p>
                </div>
            """, unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

        # Leaderboard Section with premium styling
        st.markdown("""
            <div style='background: rgba(255,255,255,0.15); backdrop-filter: blur(20px); 
                 border-radius: 30px; padding: 40px; margin: 40px auto; max-width: 900px;
                 box-shadow: 0 20px 60px rgba(0,0,0,0.3); animation: slideInUp 0.7s ease-out;'>
                <h2 style='font-family: "Montserrat", sans-serif; font-weight: 900; font-size: 36px; 
                     margin-bottom: 30px; color: white; text-shadow: 0 4px 8px rgba(0,0,0,0.3); 
                     text-align: center;'>
                    🏆 Leaderboard 🏆
                </h2>
        """, unsafe_allow_html=True)
        
        if st.session_state.scoreboard:
            # Display top 5 players with premium styling
            for idx, (rank, uname, pts) in enumerate(st.session_state.scoreboard[:5]):
                medal = ""
                rank_bg = "rgba(255,255,255,0.95)"
                
                if rank == 1: 
                    medal = "🥇"
                    rank_bg = "linear-gradient(135deg, #FFD700 0%, #FFA500 100%)"
                    text_color = "#1a202c"
                    border = "3px solid #FFD700"
                elif rank == 2: 
                    medal = "🥈"
                    rank_bg = "linear-gradient(135deg, #C0C0C0 0%, #A8A8A8 100%)"
                    text_color = "#1a202c"
                    border = "3px solid #C0C0C0"
                elif rank == 3: 
                    medal = "🥉"
                    rank_bg = "linear-gradient(135deg, #CD7F32 0%, #B8732D 100%)"
                    text_color = "#1a202c"
                    border = "3px solid #CD7F32"
                else:
                    text_color = "#1a202c"
                    border = "2px solid rgba(255,255,255,0.3)"
                
                # Highlight current user
                if uname == st.session_state.my_username:
                    rank_bg = "linear-gradient(135deg, #667eea 0%, #764ba2 100%)"
                    text_color = "white"
                    border = "3px solid #a78bfa"
                
                # Staggered animation delay
                animation_delay = idx * 0.1
                
                st.markdown(f"""
                    <div style='background: {rank_bg}; color: {text_color}; border-radius: 18px; 
                         padding: 20px 30px; margin: 16px 0; display: flex; justify-content: space-between; 
                         align-items: center; box-shadow: 0 8px 20px rgba(0,0,0,0.25); border: {border};
                         transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                         animation: fadeIn 0.6s ease-out {animation_delay}s both;'>
                        <div style='display: flex; align-items: center; gap: 20px;'>
                            <span style='font-size: 2rem;'>{medal}</span>
                            <div>
                                <span style='font-size: 1.1rem; font-weight: 700; opacity: 0.8;'>#{rank}</span>
                                <span style='font-size: 1.4rem; font-weight: 800; margin-left: 12px;'>{uname}</span>
                            </div>
                        </div>
                        <span style='font-size: 1.6rem; font-weight: 900; text-shadow: 0 2px 4px rgba(0,0,0,0.1);'>
                            {pts} pts
                        </span>
                    </div>
                """, unsafe_allow_html=True)
        else:
            st.markdown("""
                <p style='text-align: center; color: rgba(255,255,255,0.8); font-size: 1.2rem; padding: 20px;'>
                    No scores yet. Keep answering questions!
                </p>
            """, unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)
    
    # PAGE 3: LEADERBOARD PAGE (now only used for final summary)
    elif st.session_state.connected and st.session_state.current_page == "leaderboard":
        # Restore default gradient background
        st.markdown("""
            <style>
                .stApp {
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                }
            </style>
        """, unsafe_allow_html=True)
        
        st.markdown("""
            <div style='text-align: center; padding: 80px 40px; background: rgba(255,255,255,0.15); 
                 backdrop-filter: blur(20px); color: white; border-radius: 30px; 
                 box-shadow: 0 20px 60px rgba(0,0,0,0.3); max-width: 700px; margin: 40px auto;
                 animation: scaleIn 0.6s ease-out;'>
                <div class='loader' style='margin: 0 auto 30px;'></div>
                <h1 style='font-family: "Montserrat", sans-serif; font-weight: 900; font-size: 48px; 
                     margin-bottom: 20px; text-shadow: 0 4px 8px rgba(0,0,0,0.3);'>Get Ready!</h1>
                <p style='font-size: 22px; opacity: 0.9;'>The next question is coming up...</p>
            </div>
        """, unsafe_allow_html=True)

    # PAGE 4: FINAL RESULTS (shown when disconnected or quiz ends)
    elif st.session_state.current_page == "final_results":
        # Restore default background
        st.markdown("""
            <style>
                .stApp {
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                }
            </style>
        """, unsafe_allow_html=True)
        st.markdown("""
            <div style='text-align: center; padding: 40px 20px; background: white; color: #333;
                 border-radius: 16px; box-shadow: 0 8px 16px rgba(0,0,0,0.2);'>
                <h1 style='font-family: "Montserrat", sans-serif; font-weight: 900; font-size: 48px; margin-bottom: 30px;'>🎉 Game Over! 🎉</h1>
                <h2 style='margin-bottom: 30px;'>Final Standings</h2>
            </div>
        """, unsafe_allow_html=True)
        
        # Show disconnect message if applicable
        if st.session_state.show_disconnect_message:
            st.warning("You have been disconnected from the server")
        
        # Show final leaderboard
        if st.session_state.scoreboard:
            # Podium
            podium = st.session_state.scoreboard[:3]
            cols = st.columns(len(podium))
            for i, (rank, uname, pts) in enumerate(podium):
                with cols[i]:
                    if rank == 1:
                        st.markdown(f"<h2 style='text-align: center;'>🥇 1st</h2>", unsafe_allow_html=True)
                        st.markdown(f"<h3 style='text-align: center; color: #D89E00;'>{uname}</h3>", unsafe_allow_html=True)
                        st.markdown(f"<h4 style='text-align: center;'>{pts} pts</h4>", unsafe_allow_html=True)
                    elif rank == 2:
                        st.markdown(f"<h2 style='text-align: center;'>� 2nd</h2>", unsafe_allow_html=True)
                        st.markdown(f"<h3 style='text-align: center; color: #C0C0C0;'>{uname}</h3>", unsafe_allow_html=True)
                        st.markdown(f"<h4 style='text-align: center;'>{pts} pts</h4>", unsafe_allow_html=True)
                    elif rank == 3:
                        st.markdown(f"<h2 style='text-align: center;'>🥉 3rd</h2>", unsafe_allow_html=True)
                        st.markdown(f"<h3 style='text-align: center; color: #CD7F32;'>{uname}</h3>", unsafe_allow_html=True)
                        st.markdown(f"<h4 style='text-align: center;'>{pts} pts</h4>", unsafe_allow_html=True)
            
            # Rest of leaderboard
            if len(st.session_state.scoreboard) > 3:
                st.markdown("---")
                for rank, uname, pts in st.session_state.scoreboard[3:]:
                     st.markdown(f"**#{rank}** {uname} - {pts} pts")

        
        # Show personal stats
        st.markdown("---")
        st.markdown("<h3 style='text-align: center;'>Your Final Stats</h3>", unsafe_allow_html=True)
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Correct", st.session_state.total_correct)
        with col2:
            accuracy = (st.session_state.total_correct / st.session_state.total_answered * 100) if st.session_state.total_answered > 0 else 0
            st.metric("Accuracy", f"{accuracy:.0f}%")
        with col3:
            st.metric("Best Streak", st.session_state.answer_streak)
        
        st.markdown("---")
        
        # Return to lobby button
        if st.button("Return to Lobby", type="primary", use_container_width=True):
            # Reset all session state variables to their defaults
            st.session_state.clear()
            if hasattr(st, "rerun"):
                st.rerun()
            else:
                st.experimental_rerun()

    elif st.session_state.connected:
        # Waiting room (default state between questions)
        st.markdown("""
            <div style='text-align: center; padding: 80px 40px; background: rgba(255,255,255,0.15); 
                 backdrop-filter: blur(20px); color: white; border-radius: 30px; 
                 box-shadow: 0 20px 60px rgba(0,0,0,0.3); max-width: 700px; margin: 80px auto;
                 animation: scaleIn 0.6s ease-out;'>
                <div class='loader' style='margin: 0 auto 30px;'></div>
                <h1 style='font-family: "Montserrat", sans-serif; font-weight: 900; font-size: 52px; 
                     margin-bottom: 20px; text-shadow: 0 4px 8px rgba(0,0,0,0.3);'>Get Ready!</h1>
                <p style='font-size: 24px; opacity: 0.9; font-weight: 600;'>Waiting for the next question...</p>
            </div>
        """, unsafe_allow_html=True)
    else:
        # Welcome screen
        st.markdown("""
            <div style='text-align: center; padding: 80px 40px; background: rgba(255,255,255,0.95); color: #1a202c;
                 border-radius: 30px; box-shadow: 0 20px 60px rgba(0,0,0,0.4); max-width: 900px; margin: 60px auto;
                 animation: scaleIn 0.8s ease-out;'>
                <h1 style='font-family: "Montserrat", sans-serif; font-weight: 900; font-size: 64px; 
                     margin-bottom: 20px; background: linear-gradient(135deg, #667eea, #764ba2); 
                     -webkit-background-clip: text; -webkit-text-fill-color: transparent;
                     background-clip: text;'>
                    Welcome to QuizNet!
                </h1>
                <p style='font-size: 22px; margin-bottom: 40px; color: #4a5568; font-weight: 600;'>
                    Test your knowledge of TCP and UDP protocols in a fun, fast-paced quiz.
                </p>
                <div style='background: linear-gradient(135deg, #667eea, #764ba2); 
                     color: white; padding: 35px; border-radius: 20px; max-width: 500px; margin: 0 auto;
                     box-shadow: 0 12px 30px rgba(102, 126, 234, 0.3);'>
                    <h3 style='margin: 0 0 20px 0; font-weight: 800; font-size: 28px;'>How to Play:</h3>
                    <ul style='text-align: left; margin: 0; padding-left: 25px; font-size: 18px; line-height: 2;'>
                        <li>Join a game from the Lobby</li>
                        <li>Answer questions as fast as you can</li>
                        <li>Faster answers earn more points!</li>
                        <li>Climb the leaderboard and claim victory 🏆</li>
                    </ul>
                </div>
            </div>
        """, unsafe_allow_html=True)

# Auto-rerun loop
if st.session_state.connected:
    time.sleep(0.2)
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()

