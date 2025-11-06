"""
TCP quiz game server for the Transport Layer Kahoot-style project.

Responsibilities:
- Load quiz questions from questions.txt.
- Accept TCP connections from multiple players.
- Handle join requests and per-player answers.
- Orchestrate the quiz flow (ask questions, time-limit, scoring).
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
   - Store last_answer for that player.

4. game_loop():
   - Wait for players to join.
   - Wait for the operator to type "start" (or press Enter).
   - Iterate through QUESTIONS and call ask_question().

5. ask_question():
   - Broadcast the question.
   - For QUESTION_TIMEOUT seconds, check who answers first correctly.
   - Update scores, broadcast TIMEUP + winner + updated leaderboard.
"""

import os
import socket
import threading
import time
from typing import Dict, List, Tuple

HOST = "0.0.0.0"          # Listen on all interfaces
PORT = 8888
QUESTION_TIMEOUT = 30      # Seconds for each question
ENCODING = "utf-8"

# ====== QUIZ QUESTIONS (loaded from file) ======
QUESTIONS: List[Dict] = []

# ====== SERVER STATE ======
clients_lock = threading.Lock()
players: List[Dict] = []       # All connected players
scores: Dict[str, int] = {}    # username -> points
server_running = True


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
                return

        # 3) Register the new player
        player = {
            "sock": conn,
            "username": username,
            "alive": True,
            "last_answer": None,
            "ip": client_ip,
        }
        players.append(player)
        scores.setdefault(username, 0)

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
                continue

            # Message format: "answer:<A/B/C/D>"
            if line.startswith("answer:"):
                ans = line.split(":", 1)[1].strip().upper()
                with clients_lock:
                    player["last_answer"] = ans
                print(f"[SERVER] {username} answered {ans}")

    print(f"[SERVER] {username} disconnected.")
    with clients_lock:
        player["alive"] = False
    conn.close()


# ---------- Quiz logic ----------


def ask_question(question: Dict) -> None:
    """
    Broadcast a single question and wait for responses.

    Logic:
    - If no players are alive, skip the question.
    - Broadcast "question:<id>:<text>" to all clients.
    - Reset each player's last_answer.
    - Until timeout:
        * Check for disconnected players.
        * If someone answers correctly and no winner yet:
          - Declare them the winner.
          - Increment their score.
    - After timeout, broadcast:
        "broadcast:TIMEUP Correct=<correct> Winner=<winner-or-None>"
      then broadcast the updated leaderboard.
    """
    if not any_alive_players():
        print("[SERVER] Skipping question, no connected players.")
        return

    qid = question["id"]
    text = question["text"]
    correct = question["correct_option"].strip().upper()

    broadcast(f"question:{qid}:{text}")
    print(f"\n[SERVER] Question {qid}: {text}")

    # Reset last_answer for all players
    with clients_lock:
        for p in players:
            p["last_answer"] = None

    winner = None
    deadline = time.time() + QUESTION_TIMEOUT

    while time.time() < deadline:
        time.sleep(0.05)
        remove_dead_clients()

        # If everyone disconnected in the middle of the question, stop
        if not any_alive_players():
            print("[SERVER] All players disconnected during question.")
            break

        # Check if someone answered correctly and first
        with clients_lock:
            for p in players:
                if not p["alive"]:
                    continue
                ans = p["last_answer"]
                if ans and winner is None:
                    if ans == correct:
                        winner = p["username"]
                        scores[winner] = scores.get(winner, 0) + 1
                        print(
                            "[SERVER] ✅ "
                            f"{winner} answered first correctly ({ans})"
                        )

    # Announce results (even if no winner)
    if winner:
        result_msg = (
            f"broadcast:TIMEUP Correct={correct} Winner={winner}"
        )
    else:
        result_msg = (
            f"broadcast:TIMEUP Correct={correct} Winner=None"
        )

    broadcast(result_msg)

    lb = leaderboard_text()
    broadcast(lb)
    print(f"[SERVER] Leaderboard => {lb}")


def game_loop() -> None:
    """
    Main quiz flow — started manually by the server operator.

    Steps:
    1. Wait until at least one player is alive.
    2. Prompt the operator to type 'start' (or press Enter) to begin
       or 'quit' to stop.
    3. Run through all QUESTIONS, calling ask_question() for each.
    4. At the end, broadcast QUIZ_END and the final leaderboard.
    """
    global server_running

    print("[SERVER] Waiting for at least one player to join...")
    broadcast("broadcast:LOBBY Waiting for players...")

    # Wait until at least one player is alive (or server is stopped)
    while server_running and not any_alive_players():
        time.sleep(0.5)

    if not server_running:
        print("[SERVER] Stopping before quiz start (server stopped).")
        return

    print(
        "[SERVER] At least one player connected "
        f"({alive_player_count()} currently)."
    )
    print("[SERVER] Type 'start' (or just press Enter) to begin the quiz.")
    print(
        "[SERVER] Or type 'quit' / 'exit' / 'stop' to cancel and "
        "shut down the server."
    )

    # Wait for admin command on stdin
    while server_running:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            cmd = "quit"

        if cmd in ("", "start"):
            break
        if cmd in ("q", "quit", "exit", "stop"):
            print(
                "[SERVER] Stop command received. "
                "No quiz will be started."
            )
            server_running = False
            return

        print(
            "[SERVER] Unknown command. Type 'start' or press Enter to begin, "
            "'quit' to stop."
        )

    if not server_running:
        return

    # Make sure we actually have questions
    if not QUESTIONS:
        print("[SERVER] ERROR: No questions loaded. Check questions.txt.")
        broadcast("broadcast:No questions available. Game cancelled.")
        return

    print("[SERVER] Starting quiz now!")
    broadcast("broadcast:QUIZ_START")

    for q in QUESTIONS:
        ask_question(q)
        time.sleep(2)  # Short pause between questions

    broadcast("broadcast:QUIZ_END")
    broadcast(leaderboard_text())
    print("[SERVER] Quiz finished. Final leaderboard sent.")


def main() -> None:
    """
    Entry point for the TCP quiz server.

    - Loads questions from file.
    - Creates a listening TCP socket.
    - Starts accept_loop() in a background thread to handle new clients.
    - Runs game_loop() to manage the quiz lifecycle.
    - On KeyboardInterrupt or stop, shuts down the server gracefully.
    """
    global server_running

    load_questions_from_file()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(10)

    print(f"[SERVER] Listening on {HOST}:{PORT}")

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
                # Socket closed or server shutting down
                break

            threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True,
            ).start()

    # Start the accept loop in the background
    threading.Thread(target=accept_loop, daemon=True).start()

    try:
        game_loop()
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down...")

    server_running = False
    srv.close()
    print("[SERVER] Stopped.")


if __name__ == "__main__":
    main()
