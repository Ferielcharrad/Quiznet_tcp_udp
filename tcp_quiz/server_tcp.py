"""
TCP quiz game server for the Kahoot-style Transport Layer Quiz.

ENHANCED FEATURES:
- Time-based scoring: 500-1000 points (faster answers = more points)
- Track answer timestamps for bonus calculation
- Streak tracking support
- Rich feedback messages
- Professional leaderboard broadcasting

Responsibilities:
- Load quiz questions from questions.txt.
- Accept TCP connections from multiple players.
- Handle join requests and per-player answers.
- Orchestrate the quiz flow (ask questions, time-limit, scoring).
- Calculate time-based bonus points (Kahoot-style).
- Broadcast questions, results, and leaderboard to all clients.

Control flow (high level):
1. main():
   - Load questions.
   - Start TCP listening socket.
   - Start accept_loop() thread to handle new clients.
   - Run game_loop() to control quiz start and question sequence.

2. accept_loop():
   - For each new TCP connection, start handle_client() in a thread.

3. handle_client():
   - Read initial "join:<username>" message.
   - Register the player, then listen for "answer:<X>" messages.
   - Store last_answer and answer_time for that player.

4. game_loop():
   - Wait for players to join.
   - Wait for the operator to type "start" (or press Enter).
   - Iterate through QUESTIONS and call ask_question().

5. ask_question():
   - Broadcast the question with timestamp.
   - For QUESTION_TIMEOUT seconds, check answers.
   - Calculate time-based bonus points (500-1000 range).
   - Track first correct answer as winner.
   - Update scores with time bonus.
   - Broadcast TIMEUP + winner + points earned + updated leaderboard.
"""

import os
import socket
import threading
import time
from typing import Dict, List, Tuple

HOST = "0.0.0.0"          # Listen on all interfaces
PORT = 8888
QUESTION_TIMEOUT = 15      # Default seconds for each question
ENCODING = "utf-8"

# ====== QUIZ QUESTIONS (loaded from file) ======
QUESTIONS: List[Dict] = []

# ====== SERVER STATE ======
clients_lock = threading.Lock()
players: List[Dict] = []       # All connected players
scores: Dict[str, int] = {}    # username -> points
streaks: Dict[str, int] = {}   # username -> current streak count
server_running = True
skip_to_next = False           # Host can skip to next question


# ---------- Kahoot-Style Scoring Functions ----------


def calculate_time_bonus(time_taken: float, max_time: float = QUESTION_TIMEOUT) -> int:
    """
    Calculate bonus points based on answer speed (Kahoot-style).
    
    Args:
        time_taken: Seconds taken to answer
        max_time: Maximum allowed time (default: QUESTION_TIMEOUT)
    
    Returns:
        Points between 500-1000 based on speed
        - Instant answer: ~1000 points
        - Last second: ~500 points
        - Linear scaling in between
    """
    if time_taken >= max_time:
        return 500  # Minimum points for correct answer
    
    # Calculate time ratio (how much time is LEFT)
    time_ratio = 1.0 - (time_taken / max_time)
    
    # Linear scaling: 500 base + up to 500 bonus for speed
    bonus = int(500 + (time_ratio * 500))
    
    return max(500, min(1000, bonus))  # Clamp between 500-1000


def format_points_message(points: int) -> str:
    """
    Format a congratulatory message based on points earned.
    
    Args:
        points: Points earned (500-1000)
    
    Returns:
        Encouraging message string
    """
    if points >= 950:
        return f"🔥 AMAZING! +{points} pts (Lightning fast!)"
    elif points >= 850:
        return f"⚡ EXCELLENT! +{points} pts (Very quick!)"
    elif points >= 750:
        return f"✨ GREAT! +{points} pts (Nice speed!)"
    elif points >= 650:
        return f"👍 GOOD! +{points} pts (Well done!)"
    else:
        return f"✅ CORRECT! +{points} pts"


def listen_for_host_commands():
    """
    Background thread to listen for host commands during questions.
    Allows host to type 'skip' to move to next question.
    """
    global skip_to_next, server_running
    
    try:
        import msvcrt
        input_buffer = ""
        
        while server_running:
            try:
                # Check if a key was pressed
                if msvcrt.kbhit():
                    char = msvcrt.getch()
                    
                    # Handle different key codes
                    if char == b'\r':  # Enter key
                        if input_buffer.strip().lower() == "skip":
                            skip_to_next = True
                            print("\n[HOST] ⏭️  Skipping to next question...")
                        input_buffer = ""  # Reset buffer
                    elif char == b'\x08':  # Backspace
                        if input_buffer:
                            input_buffer = input_buffer[:-1]
                            # Visual feedback for backspace
                            print('\b \b', end='', flush=True)
                    elif char in (b'\x03', b'\x1b'):  # Ctrl+C or ESC
                        input_buffer = ""
                    else:
                        # Try to decode the character
                        try:
                            decoded = char.decode('utf-8', errors='ignore')
                            if decoded.isprintable():
                                input_buffer += decoded
                                # Echo the character
                                print(decoded, end='', flush=True)
                        except:
                            pass
                
                time.sleep(0.05)  # Small delay to reduce CPU usage
            except Exception as e:
                # Ignore errors in input handling
                time.sleep(0.5)
    except ImportError:
        # msvcrt not available (not Windows)
        print("[SERVER] Host skip command not available on this platform")
        while server_running:
            time.sleep(1)


# ---------- Load questions from questions.txt ----------


def load_questions_from_file() -> None:
    """
    Load questions from ../questions.txt relative to this file.

    Each non-empty, non-comment line in questions.txt must have the form:
        <question text>|<correct_option_letter>

    Example line:
        Which protocol is connection-oriented and guarantees reliability?
        A) TCP  B) UDP|A

    The loaded questions are stored in the global QUESTIONS list as:
        {
            "id": <int>,
            "text": <str>,
            "correct_option": <"A"|"B"|"C"|"D">,
        }
    """
    global QUESTIONS
    QUESTIONS = []

    base_dir = os.path.dirname(__file__)
    qpath = os.path.abspath(os.path.join(base_dir, "..", "questions.txt"))

    if not os.path.exists(qpath):
        print(f"[SERVER] ERROR: questions.txt not found at {qpath}")
        return

    qid = 1
    with open(qpath, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()

            # Skip empty lines and comment lines
            if not line or line.startswith("#"):
                continue

            parts = line.split("|")
            if len(parts) != 2:
                print(f"[SERVER] Skipping invalid question line: {line}")
                continue

            text = parts[0].strip()
            correct = parts[1].strip().upper()

            if correct not in {"A", "B", "C", "D"}:
                print(
                    "[SERVER] Skipping, invalid correct option "
                    f"'{correct}' in: {line}"
                )
                continue

            QUESTIONS.append(
                {
                    "id": qid,
                    "text": text,
                    "correct_option": correct,
                }
            )
            qid += 1

    print(f"[SERVER] Loaded {len(QUESTIONS)} questions from {qpath}")


# ---------- Utility functions ----------


def safe_send(sock: socket.socket, message: str) -> None:
    """
    Send one line to a client safely.

    The message is terminated by a newline, so clients can use line-based
    parsing. Any socket error is silently ignored (client may be gone).
    """
    try:
        sock.sendall((message + "\n").encode(ENCODING))
    except OSError:
        pass


def broadcast(message: str) -> None:
    """
    Send a line to all currently connected (alive) clients.

    Uses clients_lock to iterate over players safely.
    """
    with clients_lock:
        for player in players:
            if player["alive"]:
                safe_send(player["sock"], message)


def remove_dead_clients() -> None:
    """
    Mark disconnected clients as not alive.

    Technique:
    - Try to send empty bytes b"" to each alive client's socket.
    - If an OSError is raised, mark that client as not alive.
    """
    with clients_lock:
        for player in players:
            if not player["alive"]:
                continue
            try:
                # Sending empty bytes just to check if socket is still OK
                player["sock"].sendall(b"")
            except OSError:
                player["alive"] = False


def any_alive_players() -> bool:
    """Return True if at least one player is currently alive/connected."""
    with clients_lock:
        return any(player["alive"] for player in players)


def alive_player_count() -> int:
    """Return the number of currently alive/connected players."""
    with clients_lock:
        return sum(1 for player in players if player["alive"])


def all_players_answered() -> bool:
    """Check if all alive players have submitted an answer."""
    with clients_lock:
        for player in players:
            if player["alive"] and player["last_answer"] is None:
                return False
        return True


def leaderboard_text() -> str:
    """
    Return scoreboard in the format expected by clients.

    Format:
        "score:EMPTY:0"          if there are no scores yet
        "score:user1:3|user2:2"  otherwise (sorted by points desc)
    """
    if not scores:
        return "score:EMPTY:0"

    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    parts = [f"{uname}:{pts}" for uname, pts in ordered]
    return "score:" + "|".join(parts)


# ---------- Per-client handler ----------


def handle_client(conn: socket.socket, addr: Tuple[str, int]) -> None:
    """
    Handle communication with one TCP client.

    Steps:
    1. Read the very first message, which should be "join:<username>".
    2. Validate that the username is not already in use.
    3. Register the player and broadcast that they joined.
    4. In a loop, read lines and look for "answer:<A/B/C/D>" messages.
       - Store the latest answer in player["last_answer"].
    5. When connection ends, mark player as not alive.
    """
    print(f"[SERVER] New connection from {addr}")

    try:
        raw_bytes = conn.recv(4096)
    except OSError:
        conn.close()
        return

    if not raw_bytes:
        conn.close()
        return

    raw = raw_bytes.decode(ENCODING, errors="ignore")

    first_line = raw.strip().splitlines()[0] if raw.strip() else ""
    if first_line.startswith("join:"):
        username = first_line.split(":", 1)[1].strip()
    else:
        username = first_line.strip() or f"{addr[0]}:{addr[1]}"

    with clients_lock:
        client_ip = addr[0]

        # 1) Reject duplicate username
        for p in players:
            if p["alive"] and p["username"] == username:
                safe_send(conn, "error:username_taken")
                print(
                    "[SERVER] Username "
                    f"'{username}' already taken, rejecting {addr}"
                )
                conn.close()
                return

        # 2) Reject second connection from the same IP (same machine)
        for p in players:
            if p["alive"] and p.get("ip") == client_ip:
                safe_send(conn, "error:ip_exists")
                print(
                    f"[SERVER] IP {client_ip} already connected, "
                    f"rejecting {username} {addr}"
                )
                conn.close()
                return        # 3) Register the new player
        player = {
            "sock": conn,
            "username": username,
            "alive": True,
            "last_answer": None,
            "answer_time": None,  # Track when they answered
            "ip": client_ip,
        }
        players.append(player)
        scores.setdefault(username, 0)
        streaks.setdefault(username, 0)  # Initialize streak tracking

    print(f"[SERVER] {username} joined.")
    broadcast(f"broadcast:{username} joined the game")

    # There might be extra data after the first line in the initial buffer
    buffer = "\n".join(raw.splitlines()[1:])

    # Listen for messages from this client as long as server is running
    while server_running:
        if not buffer:
            try:
                chunk = conn.recv(4096)
            except OSError:
                break

            if not chunk:
                break

            buffer += chunk.decode(ENCODING, errors="ignore")

        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue            # Message format: "answer:<A/B/C/D>"
            if line.startswith("answer:"):
                ans = line.split(":", 1)[1].strip().upper()
                with clients_lock:
                    player["last_answer"] = ans
                    player["answer_time"] = time.time()  # Record answer timestamp
                print(f"[SERVER] {username} answered {ans}")

    print(f"[SERVER] {username} disconnected.")
    with clients_lock:
        player["alive"] = False
    conn.close()


# ---------- Quiz logic ----------


def ask_question(question: Dict, question_timeout: int) -> None:
    """
    Broadcast a single question and wait for responses with KAHOOT-STYLE SCORING.

    Enhanced Logic:
    - If no players are alive, skip the question.
    - Broadcast "question:<id>:<text>" to all clients.
    - Record question start time.
    - Reset each player's last_answer and answer_time.
    - Until timeout:
        * Check for disconnected players.
        * Track all players who answer correctly with their timestamps.
        * Calculate time-based bonus points (500-1000) for each correct answer.
        * First correct answer is the "winner" (gets special recognition).
        * Update streaks: +1 for correct, reset to 0 for wrong.
    - After timeout, broadcast:
        * Winner announcement with points earned
        * Individual feedback to all players who answered
        * Updated leaderboard
    """
    if not any_alive_players():
        print("[SERVER] Skipping question, no connected players.")
        return

    qid = question["id"]
    text = question["text"]
    correct = question["correct_option"].strip().upper()

    # Broadcast question to all clients
    broadcast(f"question:{qid}:{question_timeout}:{text}")
    print(f"\n[SERVER] Question {qid}: {text}")

    # Record start time and reset player states for this question
    question_start_time = time.time()
    
    # Reset skip flag for this question
    global skip_to_next
    skip_to_next = False

    # Reset last_answer and answer_time for all players
    with clients_lock:
        for p in players:
            p["last_answer"] = None
            p["answer_time"] = None

    # Track results for this question
    winner = None
    winner_points = 0
    correct_answers = []  # List of (username, points, time_taken)
    wrong_answers = []    # List of usernames who answered incorrectly
    
    deadline = time.time() + question_timeout
    last_broadcast_time = -1  # Track last timer broadcast    # Monitor answers until timeout OR all players answer OR host skip
    while time.time() < deadline:
        time.sleep(0.05)
        remove_dead_clients()
        
        # Broadcast remaining time every second
        remaining = int(deadline - time.time())
        if remaining >= 0 and remaining != last_broadcast_time:
            broadcast(f"timer:{remaining}")
            last_broadcast_time = remaining
        
        # Check if server is shutting down (Ctrl+C)
        if not server_running:
            print("\n[SERVER] Server shutdown requested. Stopping quiz...")
            break
        
        # Check if host wants to skip
        if skip_to_next:
            print("[SERVER] Host skipped to next question!")
            break

        # If everyone disconnected in the middle of the question, stop
        if not any_alive_players():
            print("[SERVER] All players disconnected during question.")
            break

        # Check all players' answers FIRST (before checking if all answered)
        with clients_lock:
            for p in players:
                if not p["alive"]:
                    continue
                
                ans = p["last_answer"]
                ans_time = p["answer_time"]
                username = p["username"]
                
                # Skip if player hasn't answered yet
                if ans is None or ans_time is None:
                    continue
                
                # Skip if we already processed this answer
                if username in [u for u, _, _ in correct_answers] or username in wrong_answers:
                    continue
                
                # Calculate time taken to answer
                time_taken = ans_time - question_start_time
                
                if ans == correct:
                    # CORRECT ANSWER - Calculate time-based bonus
                    points = calculate_time_bonus(time_taken, question_timeout)
                    correct_answers.append((username, points, time_taken))
                    
                    # First correct answer is the winner
                    if winner is None:
                        winner = username
                        winner_points = points
                        streaks[username] = streaks.get(username, 0) + 1
                        print(f"[SERVER] 🏆 {winner} answered first! +{points} pts (in {time_taken:.2f}s)")
                    else:
                        streaks[username] = streaks.get(username, 0) + 1
                        print(f"[SERVER] ✅ {username} also correct! +{points} pts (in {time_taken:.2f}s)")
                    
                    # Update score
                    scores[username] = scores.get(username, 0) + points
                else:
                    # WRONG ANSWER
                    wrong_answers.append(username)
                    streaks[username] = 0  # Reset streak
                    print(f"[SERVER] ❌ {username} answered {ans} (incorrect)")

        # AFTER processing answers, check if all alive players have answered
        if all_players_answered():
            print("[SERVER] ✅ All players answered! Auto-advancing...")
            time.sleep(0.5)  # Brief pause before results
            break    # === BROADCAST RESULTS ===
    
    print(f"\n[SERVER] Time's up! Results:")
    
    # Show results page
    broadcast("show:results")
    time.sleep(0.3)
    
    # 1. Announce winner (or no winner)
    if winner:
        winner_msg = f"broadcast:TIMEUP Correct={correct} Winner={winner} Points={winner_points}"
        broadcast(winner_msg)
        print(f"[SERVER] Winner: {winner} with {winner_points} points!")
    else:
        no_winner_msg = f"broadcast:TIMEUP Correct={correct} Winner=None"
        broadcast(no_winner_msg)
        print(f"[SERVER] No correct answers")

    # 2. Send individual feedback to all players
    with clients_lock:
        for p in players:
            if not p["alive"]:
                continue
                
            username = p["username"]
            
            # Check if they got it correct
            if username in [u for u, _, _ in correct_answers]:
                # Find their points and time
                for u, pts, t in correct_answers:
                    if u == username:
                        feedback = f"feedback:{username}:correct:{pts}:{t:.1f}"
                        safe_send(p["sock"], feedback)
                        break
            elif username in wrong_answers:
                feedback = f"feedback:{username}:wrong:0:0"
                safe_send(p["sock"], feedback)
            else:
                # Unanswered
                feedback = f"feedback:{username}:timeout:0:0"
                safe_send(p["sock"], feedback)
    
    # 3. Handle unanswered players (treat as incorrect - reset streak)
    unanswered = []
    with clients_lock:
        for p in players:
            if p["alive"]:
                uname = p["username"]
                if uname not in [u for u, _, _ in correct_answers] and uname not in wrong_answers:
                    unanswered.append(uname)
                    streaks[uname] = 0  # Reset streak for not answering    # Wait for players to see results
    time.sleep(3)
    
    # 4. Show leaderboard page
    broadcast("show:leaderboard")
    time.sleep(0.3)
    
    # 5. Broadcast updated leaderboard
    lb = leaderboard_text()
    broadcast(lb)
    
    # Wait for players to see leaderboard
    time.sleep(3)
    
    # Print summary
    print(f"[SERVER] Question complete:")
    print(f"  - Correct: {len(correct_answers)} player(s)")
    print(f"  - Wrong: {len(wrong_answers)} player(s)")
    print(f"  - No answer: {len(unanswered)} player(s)")
    print(f"[SERVER] Leaderboard => {lb}")


def game_loop() -> None:
    """
    Main quiz flow — started manually by the server operator.

    Enhanced Steps:
    1. Wait until at least one player is alive.
    2. Prompt the operator to type 'start' (or press Enter) to begin
       or 'quit' to stop.
    3. Run through all QUESTIONS, calling ask_question() for each.
    4. At the end, broadcast QUIZ_END, final leaderboard, and statistics.
    5. Display final results with top 3 podium.
    """
    global server_running

    print("[SERVER] ═══════════════════════════════════════════")
    print("[SERVER] 🎮 KAHOOT-STYLE QUIZ SERVER")
    print("[SERVER] ═══════════════════════════════════════════")
    print("[SERVER] Waiting for at least one player to join...")
    broadcast("broadcast:LOBBY Waiting for players...")

    # Wait until at least one player is alive (or server is stopped)
    while server_running and not any_alive_players():
        time.sleep(0.5)

    if not server_running:
        print("[SERVER] Stopping before quiz start (server stopped).")
        return

    player_count = alive_player_count()
    print(f"[SERVER] ✅ {player_count} player(s) connected!")
    print("[SERVER] ───────────────────────────────────────────")
    with clients_lock:
        for p in players:
            if p["alive"]:
                print(f"[SERVER]   👤 {p['username']}")
    print("[SERVER] ───────────────────────────────────────────")
    print(f"[SERVER] 📝 {len(QUESTIONS)} questions loaded")
    print(f"[SERVER] 🏆 500-1000 points per correct answer (speed bonus!)")
    print("[SERVER] ═══════════════════════════════════════════")
    print()

    # Ask for question timeout
    question_timeout_seconds = QUESTION_TIMEOUT
    while server_running:
        try:
            raw_timeout = input(f"[HOST] Enter question timeout in seconds (default: {QUESTION_TIMEOUT}): ").strip()
            if not raw_timeout:
                break  # Use default
            
            timeout_val = int(raw_timeout)
            if timeout_val > 0:
                question_timeout_seconds = timeout_val
                break
            else:
                print("[SERVER] Please enter a positive number.")
        except ValueError:
            print("[SERVER] Invalid input. Please enter a number.")
        except (EOFError, KeyboardInterrupt):
            print("\n[SERVER] Stop command received. No quiz will be started.")
            server_running = False
            return

    if not server_running:
        return

    print(f"[SERVER] ⏱️  Question timeout set to {question_timeout_seconds} seconds.")
    print("[SERVER] ═══════════════════════════════════════════")
    print("[SERVER] Type 'start' (or just press Enter) to begin the quiz.")
    print("[SERVER] Or type 'quit' / 'exit' / 'stop' to cancel.")
    print()

    # Wait for admin command on stdin
    while server_running:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[SERVER] ⚠️  Shutdown requested.")
            server_running = False
            return

        if cmd in ("", "start"):
            break
        if cmd in ("q", "quit", "exit", "stop"):
            print("[SERVER] Stop command received. No quiz will be started.")
            server_running = False
            return

        print("[SERVER] Unknown command. Type 'start' or press Enter to begin, 'quit' to stop.")

    if not server_running:
        return

    # Make sure we actually have questions
    if not QUESTIONS:
        print("[SERVER] ERROR: No questions loaded. Check questions.txt.")
        broadcast("broadcast:No questions available. Game cancelled.")
        return

    print()
    print("[SERVER] ═══════════════════════════════════════════")
    print("[SERVER] 🚀 STARTING QUIZ NOW!")
    print("[SERVER] ═══════════════════════════════════════════")
    broadcast("broadcast:QUIZ_START Get ready!")
    time.sleep(2)

    # Limit quiz to first 10 questions
    questions_to_ask = QUESTIONS[:10]
    
    # Run through questions (limited to 10)
    for i, q in enumerate(questions_to_ask, 1):
        # Check if server is shutting down (Ctrl+C)
        if not server_running:
            print("\n[SERVER] ⚠️  Server shutdown requested. Stopping quiz...")
            broadcast("broadcast:Server shutting down. Quiz ended.")
            break
        
        # Check if any players are still connected before each question
        if not any_alive_players():
            print("\n[SERVER] ⚠️  All players disconnected. Stopping quiz...")
            broadcast("broadcast:All players left. Quiz ended.")
            break
        
        print(f"\n[SERVER] ━━━━━━━━━━ Question {i}/{len(questions_to_ask)} ━━━━━━━━━━")
        ask_question(q, question_timeout_seconds)
        
        # Check if server is shutting down after question
        if not server_running:
            print("\n[SERVER] ⚠️  Server shutdown requested. Stopping quiz...")
            broadcast("broadcast:Server shutting down. Quiz ended.")
            break
        
        # Check again after question in case players left during results
        if not any_alive_players():
            print("\n[SERVER] ⚠️  All players disconnected. Stopping quiz...")
            break
          # Small pause before next question (results+leaderboard already shown for 6 seconds)
        time.sleep(1)

    # Quiz finished - check if completed or terminated early
    print()
    print("[SERVER] ═══════════════════════════════════════════")
    
    if any_alive_players():
        print("[SERVER] 🎉 QUIZ COMPLETE!")
        print("[SERVER] ═══════════════════════════════════════════")
        broadcast("broadcast:QUIZ_END Great game everyone!")
        
        # Display final results
        time.sleep(1)
        display_final_results()
        
        # Send final leaderboard
        lb = leaderboard_text()
        broadcast(lb)
        print("[SERVER] Final leaderboard sent to all players.")
    else:
        print("[SERVER] ⚠️  QUIZ TERMINATED (No players remaining)")
        print("[SERVER] ═══════════════════════════════════════════")
        # Still show final stats for server console
        display_final_results()
    
    print("[SERVER] ═══════════════════════════════════════════")


def display_final_results() -> None:
    """
    Display final quiz results with podium and statistics.
    """
    print()
    print("[SERVER] ╔═══════════════════════════════════════╗")
    print("[SERVER] ║         FINAL LEADERBOARD             ║")
    print("[SERVER] ╚═══════════════════════════════════════╝")
    print()
    
    if not scores:
        print("[SERVER]   No scores recorded.")
        return
    
    # Sort by score descending
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    # Display top 3 with podium
    for rank, (username, points) in enumerate(ranked[:3], 1):
        streak = streaks.get(username, 0)
        if rank == 1:
            print(f"[SERVER]   🥇 1st: {username} - {points} pts (Streak: {streak})")
        elif rank == 2:
            print(f"[SERVER]   🥈 2nd: {username} - {points} pts (Streak: {streak})")
        elif rank == 3:
            print(f"[SERVER]   🥉 3rd: {username} - {points} pts (Streak: {streak})")
    
    # Display rest
    if len(ranked) > 3:
        print()
        for rank, (username, points) in enumerate(ranked[3:], 4):
            streak = streaks.get(username, 0)
            print(f"[SERVER]   {rank}. {username} - {points} pts (Streak: {streak})")
    
    print()
    print("[SERVER] ═══════════════════════════════════════════")
    
    # Statistics
    total_players = len(ranked)
    avg_score = sum(scores.values()) / total_players if total_players > 0 else 0
    max_streak = max(streaks.values()) if streaks else 0
    
    print(f"[SERVER] 📊 Statistics:")
    print(f"[SERVER]   Total Players: {total_players}")
    print(f"[SERVER]   Average Score: {avg_score:.1f} pts")
    print(f"[SERVER]   Highest Streak: {max_streak}")
    print(f"[SERVER]   Total Questions: {len(QUESTIONS)}")
    print("[SERVER] ═══════════════════════════════════════════")


def main() -> None:
    """
    Entry point for the Kahoot-style TCP quiz server.

    - Loads questions from file.
    - Creates a listening TCP socket.
    - Starts accept_loop() in a background thread to handle new clients.
    - Runs game_loop() to manage the quiz lifecycle.    - On KeyboardInterrupt or stop, shuts down the server gracefully.
    """
    global server_running
    
    # Enable Ctrl+C handling
    import signal
    
    def signal_handler(sig, frame):
        global server_running
        print("\n[SERVER] ⚠️  Shutting down gracefully...")
        server_running = False
    
    signal.signal(signal.SIGINT, signal_handler)

    print()
    print("╔════════════════════════════════════════════════════════╗")
    print("║                                                        ║")
    print("║        🎮 QUIZNET - KAHOOT-STYLE TCP SERVER 🎮        ║")
    print("║                                                        ║")
    print("║           Transport Layer Quiz Competition            ║")
    print("║                                                        ║")
    print("╚════════════════════════════════════════════════════════╝")
    print()

    load_questions_from_file()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(10)

    print(f"[SERVER] 🌐 Server listening on {HOST}:{PORT}")
    print(f"[SERVER] ⏱️  Question timeout: {QUESTION_TIMEOUT} seconds")
    print(f"[SERVER] 🏆 Scoring: 500-1000 points (time-based bonus)")
    print(f"[SERVER] 📝 Questions loaded: {len(QUESTIONS)}")
    print()
    print("[SERVER] Waiting for players to connect...")
    print("[SERVER] (Press Ctrl+C to stop the server)")
    print()

    def accept_loop() -> None:
        """
        Accept new client connections and spawn a handler thread for each.

        This runs in a daemon thread and stops when server_running is False
        or the listening socket is closed.
        """
        while server_running:
            try:
                conn, addr = srv.accept()
            except OSError:
                break

            threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True,
            ).start()

    threading.Thread(target=accept_loop, daemon=True).start()

    threading.Thread(target=listen_for_host_commands, daemon=True).start()

    try:
        game_loop()
    except KeyboardInterrupt:
        print("\n[SERVER] ⚠️  Shutting down...")

    server_running = False
    srv.close()
    print()
    print("[SERVER] ═══════════════════════════════════════════")
    print("[SERVER] 👋 Server stopped. Thanks for playing!")
    print("[SERVER] ═══════════════════════════════════════════")
    print()


if __name__ == "__main__":
    main()
