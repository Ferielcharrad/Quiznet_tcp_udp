import socket
import time
import threading
import os
import sys
import msvcrt  # ✅ for Windows non-blocking key input
from typing import Dict, Tuple, List

SERVER_IP = "0.0.0.0"
SERVER_PORT = 8888
QUESTION_TIMEOUT = 10
ENCODING = "utf-8"

# ====== GLOBAL STATE ======
clients_lock = threading.Lock()
clients: List[Tuple[str, int]] = []  # list of (ip, port)
usernames: Dict[Tuple[str, int], str] = {}  # (ip,port) -> "Nour"
scores: Dict[str, int] = {}  # "Nour" -> 3

# create socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((SERVER_IP, SERVER_PORT))
sock.setblocking(False)


# ====== LOAD QUESTIONS FROM FILE ======
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


# ====== UTILITIES ======
def add_client(addr: Tuple[str, int], username: str):
    with clients_lock:
        if addr not in clients:
            clients.append(addr)
        usernames[addr] = username
        if username not in scores:
            scores[username] = 0


def broadcast(msg: str):
    data = msg.encode(ENCODING)
    with clients_lock:
        for addr in clients:
            try:
                sock.sendto(data, addr)
            except OSError:
                pass


def leaderboard_text() -> str:
    if not scores:
        return "score:EMPTY:0"
    lines = []
    for user, pts in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"{user}:{pts}")
    return "score:" + "|".join(lines)


# ====== QUIZ LOGIC ======
def ask_question(q: dict):
    qid = q["id"]
    qtext = q["text"]
    correct = q["correct_option"].strip().upper()

    print(f"\n[SERVER] Question {qid}: {qtext}")
    broadcast(f"question:{qid}:{qtext}")

    deadline = time.time() + QUESTION_TIMEOUT
    winner = None

    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except BlockingIOError:
            time.sleep(0.05)
            continue

        raw = data.decode(ENCODING, errors="ignore").strip()

        if raw.startswith("join:"):
            username = raw.split(":", 1)[1].strip()
            add_client(addr, username)
            print(f"[SERVER] {username} joined from {addr}")
            sock.sendto(f"broadcast:welcome {username}".encode(ENCODING), addr)
            continue

        if raw.startswith("answer:"):
            ans = raw.split(":", 1)[1].strip().upper()
            username = usernames.get(addr, f"{addr[0]}:{addr[1]}")
            print(f"[SERVER] {username} answered {ans}")

            if winner is None and ans == correct:
                winner = username
                scores[username] = scores.get(username, 0) + 1

    if winner:
        result_msg = f"broadcast:TIMEUP Correct={correct} Winner={winner}"
        print(f"[SERVER] Winner={winner}, correct={correct}")
    else:
        result_msg = f"broadcast:TIMEUP Correct={correct} Winner=None"
        print(f"[SERVER] No correct answer. Correct={correct}")

    broadcast(result_msg)
    broadcast(leaderboard_text())


# ====== MAIN ======
def main():
    print("[SERVER] UDP quiz server running on port", SERVER_PORT)
    print("[SERVER] Waiting for players to send: join:<username>")
    print("[SERVER] Press ENTER to start the quiz when ready.\n")

    questions = load_questions_from_file()
    if not questions:
        print("[SERVER] No questions loaded. Exiting.")
        return

    # Wait for clients and Enter key
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except BlockingIOError:
            time.sleep(0.05)
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b"\r":  # Enter key pressed
                    break
                elif ch.lower() in (b"q", b"x"):  # Quit
                    print("[SERVER] Exiting before start.")
                    return
            continue

        raw = data.decode(ENCODING, errors="ignore").strip()
        if raw.startswith("join:"):
            username = raw.split(":", 1)[1].strip()
            add_client(addr, username)
            print(f"[SERVER] {username} joined from {addr}")
            sock.sendto(f"broadcast:welcome {username}".encode(ENCODING), addr)

    print("[SERVER] Starting quiz now!")
    broadcast("broadcast:QUIZ_START")

    for q in questions:
        ask_question(q)
        time.sleep(2)

    broadcast("broadcast:QUIZ_END")
    final_lb = leaderboard_text()
    broadcast(final_lb)
    print("[SERVER] Quiz finished.")
    print("[SERVER] Final leaderboard:", final_lb)


if __name__ == "__main__":
    main()
