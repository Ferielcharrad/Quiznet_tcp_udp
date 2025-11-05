import socket
import threading
import time
import os
from typing import Dict, List, Tuple

HOST = "0.0.0.0"  # Listen on all interfaces
PORT = 8888
QUESTION_TIMEOUT = 30  # seconds for each question
ENCODING = "utf-8"

# ====== QUIZ QUESTIONS (loaded from file) ======
QUESTIONS: List[Dict] = []

# ====== SERVER STATE ======
clients_lock = threading.Lock()
players: List[Dict] = []  # all connected players
scores: Dict[str, int] = {}  # username -> points
server_running = True


# ---------- Load questions from questions.txt ----------

def load_questions_from_file():
    """
    Load questions from ../questions.txt (project root).

    Each non-empty, non-comment line must be:
        <question text>|<correct_option_letter>

    Example line:
        Which protocol is connection-oriented and guarantees reliability? A) TCP  B) UDP|A
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
            if not line or line.startswith("#"):
                continue

            parts = line.split("|")
            if len(parts) != 2:
                print(f"[SERVER] Skipping invalid question line: {line}")
                continue

            text = parts[0].strip()
            correct = parts[1].strip().upper()
            if correct not in {"A", "B", "C", "D"}:
                print(f"[SERVER] Skipping, invalid correct option '{correct}' in: {line}")
                continue

            QUESTIONS.append({
                "id": qid,
                "text": text,
                "correct_option": correct,
            })
            qid += 1

    print(f"[SERVER] Loaded {len(QUESTIONS)} questions from {qpath}")


# ---------- Utility functions ----------

def safe_send(sock: socket.socket, message: str):
    """Send one line to a client safely."""
    try:
        sock.sendall((message + "\n").encode(ENCODING))
    except OSError:
        pass


def broadcast(message: str):
    """Send a line to all connected clients."""
    with clients_lock:
        for p in players:
            if p["alive"]:
                safe_send(p["sock"], message)


def remove_dead_clients():
    """Mark disconnected clients as not alive."""
    with clients_lock:
        for p in players:
            if not p["alive"]:
                continue
            try:
                # Sending empty bytes just to check if socket is still ok
                p["sock"].sendall(b"")
            except OSError:
                p["alive"] = False


def any_alive_players() -> bool:
    """Return True if at least one player is currently alive/connected."""
    with clients_lock:
        return any(p["alive"] for p in players)


def alive_player_count() -> int:
    """Return number of currently alive/connected players."""
    with clients_lock:
        return sum(1 for p in players if p["alive"])


def leaderboard_text() -> str:
    """Return scoreboard in the format expected by clients."""
    if not scores:
        return "score:EMPTY:0"
    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    parts = [f"{uname}:{pts}" for uname, pts in ordered]
    return "score:" + "|".join(parts)


# ---------- Per-client handler ----------

def handle_client(conn: socket.socket, addr: Tuple[str, int]):
    """Handle communication with one TCP client."""
    print(f"[SERVER] New connection from {addr}")

    # IMPORTANT: no timeout here, just wait for first data (join message)
    try:
        raw_bytes = conn.recv(4096)
    except OSError:
        conn.close()
        return

    if not raw_bytes:
        conn.close()
        return

    raw = raw_bytes.decode(ENCODING, errors="ignore")

    # Expect first message like join:<username>
    first_line = raw.strip().splitlines()[0] if raw.strip() else ""
    if first_line.startswith("join:"):
        username = first_line.split(":", 1)[1].strip()
    else:
        username = first_line.strip() or f"{addr[0]}:{addr[1]}"

    # Check duplicate usernames
    with clients_lock:
        for p in players:
            if p["alive"] and p["username"] == username:
                safe_send(conn, "error:username_taken")
                print(f"[SERVER] Username '{username}' already taken, rejecting {addr}")
                conn.close()
                return

        player = {
            "sock": conn,
            "username": username,
            "alive": True,
            "last_answer": None,
        }
        players.append(player)
        scores.setdefault(username, 0)

    print(f"[SERVER] {username} joined.")
    broadcast(f"broadcast:{username} joined the game")

    # There might be extra data after the first line
    buffer = "\n".join(raw.splitlines()[1:])

    # Listen for messages
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

            # answer:<A/B/C/D>
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

def ask_question(q: dict):
    """Broadcast a question and wait for responses."""
    # Don't even ask the question if nobody is connected
    if not any_alive_players():
        print("[SERVER] Skipping question, no connected players.")
        return

    qid = q["id"]
    text = q["text"]
    correct = q["correct_option"].strip().upper()

    broadcast(f"question:{qid}:{text}")
    print(f"\n[SERVER] Question {qid}: {text}")

    with clients_lock:
        for p in players:
            p["last_answer"] = None

    winner = None
    deadline = time.time() + QUESTION_TIMEOUT

    while time.time() < deadline:
        time.sleep(0.05)
        remove_dead_clients()

        # If everyone disconnected in the middle of the question, stop waiting
        if not any_alive_players():
            print("[SERVER] All players disconnected during question.")
            break

        with clients_lock:
            for p in players:
                if not p["alive"]:
                    continue
                ans = p["last_answer"]
                if ans and winner is None:
                    if ans == correct:
                        winner = p["username"]
                        scores[winner] = scores.get(winner, 0) + 1
                        print(f"[SERVER] ✅ {winner} answered first correctly ({ans})")

    # Announce results (even if no winner)
    if winner:
        result_msg = f"broadcast:TIMEUP Correct={correct} Winner={winner}"
    else:
        result_msg = f"broadcast:TIMEUP Correct={correct} Winner=None"
    broadcast(result_msg)

    lb = leaderboard_text()
    broadcast(lb)
    print(f"[SERVER] Leaderboard => {lb}")


def game_loop():
    """Main game flow — started manually by the server operator."""
    global server_running

    print("[SERVER] Waiting for at least one player to join...")
    broadcast("broadcast:LOBBY Waiting for players...")

    # Wait until at least one player is alive (or server is stopped)
    while server_running and not any_alive_players():
        time.sleep(0.5)

    if not server_running:
        print("[SERVER] Stopping before quiz start (server stopped).")
        return

    print(f"[SERVER] At least one player connected ({alive_player_count()} currently).")
    print("[SERVER] Type 'start' (or just press Enter) to begin the quiz.")
    print("[SERVER] Or type 'quit' / 'exit' / 'stop' to cancel and shut down the server.")

    # Wait for admin command
    while server_running:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            cmd = "quit"

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

    print("[SERVER] Starting quiz now!")
    broadcast("broadcast:QUIZ_START")

    for q in QUESTIONS:
        ask_question(q)
        time.sleep(2)  # short pause between questions

    broadcast("broadcast:QUIZ_END")
    broadcast(leaderboard_text())
    print("[SERVER] Quiz finished. Final leaderboard sent.")


def main():
    global server_running

    load_questions_from_file()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(10)

    print(f"[SERVER] Listening on {HOST}:{PORT}")

    def accept_loop():
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

    try:
        game_loop()
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down...")

    server_running = False
    srv.close()
    print("[SERVER] Stopped.")


if __name__ == "__main__":
    main()
