"""
UDP quiz game server for the Kahoot-style Transport Layer Quiz.

ENHANCED FEATURES:
- Time-based scoring: 500-1000 points (faster answers = more points)
- Track answer timestamps for bonus calculation
- Streak tracking support
- Rich feedback messages
- Professional leaderboard broadcasting
- Host skip commands
- Auto-advance when all players answer
- Visual progress and statistics

Responsibilities:
- Load quiz questions from questions.txt
- Accept UDP messages from multiple players
- Handle join requests and per-player answers
- Orchestrate the quiz flow (ask questions, time-limit, scoring)
- Calculate time-based bonus points (Kahoot-style)
- Broadcast questions, results, and leaderboard to all clients
"""

import socket
import time
import threading
import os
import sys
from typing import Dict, Tuple, List

SERVER_IP = "0.0.0.0"
SERVER_PORT = 8888
QUESTION_TIMEOUT = 15  # Default seconds for each question
ENCODING = "utf-8"

# ====== GLOBAL STATE ======
clients_lock = threading.Lock()
clients: List[Tuple[str, int]] = []  # list of (ip, port)
usernames: Dict[Tuple[str, int], str] = {}  # (ip,port) -> "username"
scores: Dict[str, int] = {}  # "username" -> points
streaks: Dict[str, int] = {}  # "username" -> current streak count
answer_times: Dict[Tuple[str, int], float] = {}  # Track when each client answered
last_answers: Dict[Tuple[str, int], str] = {}  # Track last answer from each client

server_running = True
skip_to_next = False  # Host can skip to next question

# create socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((SERVER_IP, SERVER_PORT))
sock.setblocking(False)


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



def load_questions_from_file():
    """
    Load questions from ../questions.txt relative to this file.
    Each line format:
        Question text|A
    Example:
        Which protocol is connection-oriented and guarantees reliability? A) TCP  B) UDP|A
    """
    questions = []
    base_dir = os.path.dirname(__file__)
    qpath = os.path.abspath(os.path.join(base_dir, "..", "questions.txt"))

    if not os.path.exists(qpath):
        print(f"[SERVER] ERROR: questions.txt not found at {qpath}")
        return questions

    qid = 1
    with open(qpath, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) != 2:
                print(f"[SERVER] Skipping invalid question line: {line}")
                continue
            text = parts[0].strip()
            correct = parts[1].strip().upper()
            questions.append({
                "id": qid,
                "text": text,
                "correct_option": correct
            })
            qid += 1

    print(f"[SERVER] Loaded {len(questions)} questions from {qpath}")
    return questions


# ---------- Utility functions ----------


def add_client(addr: Tuple[str, int], username: str):
    """Add a new client to the game."""
    with clients_lock:
        if addr not in clients:
            clients.append(addr)
        usernames[addr] = username
        if username not in scores:
            scores[username] = 0
        if username not in streaks:
            streaks[username] = 0


def broadcast(msg: str):
    """Send a message to all connected clients."""
    data = msg.encode(ENCODING)
    with clients_lock:
        for addr in clients:
            try:
                sock.sendto(data, addr)
            except OSError:
                pass


def send_to_client(addr: Tuple[str, int], msg: str):
    """Send a message to a specific client."""
    try:
        sock.sendto(msg.encode(ENCODING), addr)
    except OSError:
        pass


def alive_client_count() -> int:
    """Return the number of currently connected clients."""
    with clients_lock:
        return len(clients)


def all_clients_answered() -> bool:
    """Check if all clients have submitted an answer."""
    with clients_lock:
        for addr in clients:
            if addr not in last_answers:
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


# ---------- Quiz logic ----------


def ask_question(q: dict, question_timeout: int) -> None:
    """
    Broadcast a single question and wait for responses with KAHOOT-STYLE SCORING.

    Enhanced Logic:
    - If no clients are connected, skip the question.
    - Broadcast "question:<id>:<timeout>:<text>" to all clients.
    - Record question start time.
    - Reset each client's answer tracking.
    - Until timeout:
        * Check for new answers from clients.
        * Calculate time-based bonus points (500-1000) for each correct answer.
        * First correct answer is the "winner" (gets special recognition).
        * Update streaks: +1 for correct, reset to 0 for wrong.
    - After timeout, broadcast:
        * Winner announcement with points earned
        * Individual feedback to all clients who answered
        * Updated leaderboard
    """
    global skip_to_next
    
    if alive_client_count() == 0:
        print("[SERVER] Skipping question, no connected clients.")
        return

    qid = q["id"]
    qtext = q["text"]
    correct = q["correct_option"].strip().upper()

    # Broadcast question to all clients
    broadcast(f"question:{qid}:{question_timeout}:{qtext}")
    print(f"\n[SERVER] Question {qid}: {qtext}")

    # Record start time and reset client states for this question
    question_start_time = time.time()
    skip_to_next = False
    
    # Reset answer tracking
    with clients_lock:
        answer_times.clear()
        last_answers.clear()

    # Track results for this question
    winner = None
    winner_points = 0
    correct_answers = []  # List of (username, points, time_taken, addr)
    wrong_answers = []    # List of (username, addr)
    
    deadline = time.time() + question_timeout
    last_broadcast_time = -1

    # Monitor answers until timeout OR all clients answer OR host skip
    while time.time() < deadline:
        # Broadcast remaining time every second
        remaining = int(deadline - time.time())
        if remaining >= 0 and remaining != last_broadcast_time:
            broadcast(f"timer:{remaining}")
            last_broadcast_time = remaining
        
        # Check if host wants to skip
        if skip_to_next:
            print("[SERVER] Host skipped to next question!")
            break
        
        # Check if server is shutting down
        if not server_running:
            print("\n[SERVER] Server shutdown requested. Stopping quiz...")
            break

        # Try to receive messages
        try:
            data, addr = sock.recvfrom(4096)
        except BlockingIOError:
            time.sleep(0.05)
            
            # Check if all clients answered
            if all_clients_answered() and alive_client_count() > 0:
                print("[SERVER] ✅ All clients answered! Auto-advancing...")
                time.sleep(0.5)
                break
            
            continue

        raw = data.decode(ENCODING, errors="ignore").strip()

        # Handle late join requests during question
        if raw.startswith("join:"):
            username = raw.split(":", 1)[1].strip()
            add_client(addr, username)
            print(f"[SERVER] {username} joined from {addr}")
            send_to_client(addr, f"broadcast:welcome {username}")
            continue

        # Handle answers
        if raw.startswith("answer:"):
            ans = raw.split(":", 1)[1].strip().upper()
            username = usernames.get(addr, f"{addr[0]}:{addr[1]}")
            
            # Record answer and time
            with clients_lock:
                if addr not in last_answers:  # Only count first answer
                    last_answers[addr] = ans
                    answer_times[addr] = time.time()
            
            # Calculate time taken
            time_taken = answer_times[addr] - question_start_time
            
            # Skip if already processed
            if username in [u for u, _, _, _ in correct_answers] or username in [u for u, _ in wrong_answers]:
                continue
            
            if ans == correct:
                # CORRECT ANSWER - Calculate time-based bonus
                points = calculate_time_bonus(time_taken, question_timeout)
                correct_answers.append((username, points, time_taken, addr))
                
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
                wrong_answers.append((username, addr))
                streaks[username] = 0  # Reset streak
                print(f"[SERVER] ❌ {username} answered {ans} (incorrect)")

    # === BROADCAST RESULTS ===
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

    # 2. Send individual feedback to all clients
    with clients_lock:
        for addr in clients:
            username = usernames.get(addr, "Unknown")
            
            # Check if they got it correct
            if username in [u for u, _, _, _ in correct_answers]:
                # Find their points and time
                for u, pts, t, a in correct_answers:
                    if u == username:
                        feedback = f"feedback:{username}:correct:{pts}:{t:.1f}"
                        send_to_client(addr, feedback)
                        break
            elif username in [u for u, _ in wrong_answers]:
                feedback = f"feedback:{username}:wrong:0:0"
                send_to_client(addr, feedback)
            else:
                # Unanswered
                feedback = f"feedback:{username}:timeout:0:0"
                send_to_client(addr, feedback)
                streaks[username] = 0  # Reset streak for not answering
    
    # Wait for clients to see results
    time.sleep(3)
    
    # 3. Show leaderboard page
    broadcast("show:leaderboard")
    time.sleep(0.3)
    
    # 4. Broadcast updated leaderboard
    lb = leaderboard_text()
    broadcast(lb)
    
    # Wait for clients to see leaderboard
    time.sleep(3)
    
    # Print summary
    unanswered_count = alive_client_count() - len(correct_answers) - len(wrong_answers)
    print(f"[SERVER] Question complete:")
    print(f"  - Correct: {len(correct_answers)} client(s)")
    print(f"  - Wrong: {len(wrong_answers)} client(s)")
    print(f"  - No answer: {unanswered_count} client(s)")
    print(f"[SERVER] Leaderboard => {lb}")


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
    total_clients = len(ranked)
    avg_score = sum(scores.values()) / total_clients if total_clients > 0 else 0
    max_streak = max(streaks.values()) if streaks else 0
    
    print(f"[SERVER] 📊 Statistics:")
    print(f"[SERVER]   Total Players: {total_clients}")
    print(f"[SERVER]   Average Score: {avg_score:.1f} pts")
    print(f"[SERVER]   Highest Streak: {max_streak}")
    print("[SERVER] ═══════════════════════════════════════════")


def game_loop(questions: List[dict]) -> None:
    """
    Main quiz flow — started manually by the server operator.

    Enhanced Steps:
    1. Wait until at least one client is connected.
    2. Prompt the operator to configure timeout and start quiz.
    3. Run through questions (limited to 10), calling ask_question() for each.
    4. At the end, broadcast QUIZ_END, final leaderboard, and statistics.
    5. Display final results with top 3 podium.
    """
    global server_running
    
    print("[SERVER] ═══════════════════════════════════════════")
    print("[SERVER] 🎮 KAHOOT-STYLE UDP QUIZ SERVER")
    print("[SERVER] ═══════════════════════════════════════════")
    print("[SERVER] Waiting for at least one client to join...")
    broadcast("broadcast:LOBBY Waiting for players...")

    # Listen for join messages in a loop
    print("[SERVER] (Press Ctrl+C to stop the server)")
    print()
    
    try:
        import msvcrt
        has_msvcrt = True
    except ImportError:
        has_msvcrt = False
        print("[SERVER] Note: msvcrt not available. Press Ctrl+C to start.")
    
    # Wait for clients
    while server_running and alive_client_count() == 0:
        try:
            data, addr = sock.recvfrom(4096)
            raw = data.decode(ENCODING, errors="ignore").strip()
            
            if raw.startswith("join:"):
                username = raw.split(":", 1)[1].strip()
                add_client(addr, username)
                print(f"[SERVER] {username} joined from {addr}")
                send_to_client(addr, f"broadcast:welcome {username}")
        except BlockingIOError:
            time.sleep(0.05)
            continue

    if not server_running:
        print("[SERVER] Stopping before quiz start (server stopped).")
        return

    client_count = alive_client_count()
    print(f"[SERVER] ✅ {client_count} client(s) connected!")
    print("[SERVER] ───────────────────────────────────────────")
    with clients_lock:
        for addr in clients:
            username = usernames.get(addr, "Unknown")
            print(f"[SERVER]   👤 {username}")
    print("[SERVER] ───────────────────────────────────────────")
    print(f"[SERVER] 📝 {len(questions)} questions loaded")
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

    # Wait for admin command
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

    print()
    print("[SERVER] ═══════════════════════════════════════════")
    print("[SERVER] 🚀 STARTING QUIZ NOW!")
    print("[SERVER] ═══════════════════════════════════════════")
    broadcast("broadcast:QUIZ_START Get ready!")
    time.sleep(2)

    # Limit quiz to first 10 questions
    questions_to_ask = questions[:10]
    
    # Run through questions (limited to 10)
    for i, q in enumerate(questions_to_ask, 1):
        # Check if server is shutting down
        if not server_running:
            print("\n[SERVER] ⚠️  Server shutdown requested. Stopping quiz...")
            broadcast("broadcast:Server shutting down. Quiz ended.")
            break
        
        # Check if any clients are still connected
        if alive_client_count() == 0:
            print("\n[SERVER] ⚠️  All clients disconnected. Stopping quiz...")
            break
        
        print(f"\n[SERVER] ━━━━━━━━━━ Question {i}/{len(questions_to_ask)} ━━━━━━━━━━")
        ask_question(q, question_timeout_seconds)
        
        # Check again after question
        if not server_running:
            print("\n[SERVER] ⚠️  Server shutdown requested. Stopping quiz...")
            broadcast("broadcast:Server shutting down. Quiz ended.")
            break
        
        if alive_client_count() == 0:
            print("\n[SERVER] ⚠️  All clients disconnected. Stopping quiz...")
            break
        
        # Small pause before next question
        time.sleep(1)

    # Quiz finished
    print()
    print("[SERVER] ═══════════════════════════════════════════")
    
    if alive_client_count() > 0:
        print("[SERVER] 🎉 QUIZ COMPLETE!")
        print("[SERVER] ═══════════════════════════════════════════")
        broadcast("broadcast:QUIZ_END Great game everyone!")
        
        # Display final results
        time.sleep(1)
        display_final_results()
        
        # Send final leaderboard
        lb = leaderboard_text()
        broadcast(lb)
        print("[SERVER] Final leaderboard sent to all clients.")
    else:
        print("[SERVER] ⚠️  QUIZ TERMINATED (No clients remaining)")
        print("[SERVER] ═══════════════════════════════════════════")
        display_final_results()
    
    print("[SERVER] ═══════════════════════════════════════════")


# ---------- Main ----------


def main():
    """
    Entry point for the Kahoot-style UDP quiz server.

    - Loads questions from file.
    - Creates a listening UDP socket.
    - Starts listen_for_host_commands() in a background thread.
    - Runs game_loop() to manage the quiz lifecycle.
    - On KeyboardInterrupt or stop, shuts down the server gracefully.
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
    print("║        🎮 QUIZNET - KAHOOT-STYLE UDP SERVER 🎮        ║")
    print("║                                                        ║")
    print("║           Transport Layer Quiz Competition            ║")
    print("║                                                        ║")
    print("╚════════════════════════════════════════════════════════╝")
    print()

    questions = load_questions_from_file()
    
    if not questions:
        print("[SERVER] ERROR: No questions loaded. Check questions.txt.")
        return

    print(f"[SERVER] 🌐 Server listening on {SERVER_IP}:{SERVER_PORT}")
    print(f"[SERVER] ⏱️  Question timeout: {QUESTION_TIMEOUT} seconds")
    print(f"[SERVER] 🏆 Scoring: 500-1000 points (time-based bonus)")
    print(f"[SERVER] 📝 Questions loaded: {len(questions)}")
    print()
    print("[SERVER] Waiting for clients to connect...")
    print("[SERVER] (Press Ctrl+C to stop the server)")
    print()

    # Start host command listener thread
    threading.Thread(target=listen_for_host_commands, daemon=True).start()

    try:
        game_loop(questions)
    except KeyboardInterrupt:
        print("\n[SERVER] ⚠️  Shutting down...")

    server_running = False
    sock.close()
    print()
    print("[SERVER] ═══════════════════════════════════════════")
    print("[SERVER] 👋 Server stopped. Thanks for playing!")
    print("[SERVER] ═══════════════════════════════════════════")
    print()


if __name__ == "__main__":
    main()
