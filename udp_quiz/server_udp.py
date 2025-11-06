# server_udp.py
import socket
import time
import threading
from typing import Dict, Tuple, List

SERVER_IP = "0.0.0.0"   # listen on all interfaces
SERVER_PORT = 8888
QUESTION_TIMEOUT = 40  # seconds to answer each question
ENCODING = "utf-8"

# ====== QUIZ DATA (you will later replace with your 10 transport-layer questions) ======
QUESTIONS = [
    {
        "id": 1,
        "text": "Which protocol is connectionless: A) TCP  B) UDP  C) Both  D) None",
        "correct_option": "B"
    },
    {
        "id": 2,
        "text": "Which one may deliver packets out of order: A) TCP  B) UDP  C) Neither",
        "correct_option": "B"
    },
    {
        "id": 3,
        "text": "Which one guarantees reliable delivery: A) TCP  B) UDP",
        "correct_option": "A"
    }
]

# ====== GLOBAL STATE ======
clients_lock = threading.Lock()
clients: List[Tuple[str, int]] = []  # list of (ip, port)
usernames: Dict[Tuple[str, int], str] = {}  # (ip,port) -> "Nour"
scores: Dict[str, int] = {}  # "Nour" -> 3

# this socket will be used by both the listener thread and main thread to send
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((SERVER_IP, SERVER_PORT))
sock.setblocking(False)  # non-blocking so main loop can poll without freezing


def add_client(addr: Tuple[str, int], username: str):
    """Register/refresh a client in the game."""
    with clients_lock:
        if addr not in clients:
            clients.append(addr)
        usernames[addr] = username
        if username not in scores:
            scores[username] = 0


def broadcast(msg: str):
    """Send a text message to all known clients."""
    data = msg.encode(ENCODING)
    with clients_lock:
        for addr in clients:
            try:
                sock.sendto(data, addr)
            except OSError:
                # ignore send errors, keep server alive
                pass


def leaderboard_text() -> str:
    if not scores:
        return "score:EMPTY:0"
    # sort by score desc
    lines = []
    for user, pts in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"{user}:{pts}")
    return "score:" + "|".join(lines)


def ask_question(q: dict):
    """
    Broadcast a question, wait for answers until timeout,
    reward first correct respondent, then broadcast results.
    """
    qid = q["id"]
    qtext = q["text"]
    correct = q["correct_option"].strip().upper()

    print(f"\n[SERVER] Question {qid}: {qtext}")
    question_msg = f"question:{qid}:{qtext}"
    broadcast(question_msg)

    # We'll listen for answers for QUESTION_TIMEOUT seconds
    deadline = time.time() + QUESTION_TIMEOUT
    winner = None  # first correct username
    received_answers = []  # (username, answer)

    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except BlockingIOError:
            # no packet right now
            time.sleep(0.05)
            continue

        raw = data.decode(ENCODING, errors="ignore").strip()
        # expected "answer:<option>"
        # BUT we also might get late "join:<username>"
        if raw.startswith("join:"):
            # register player
            username = raw.split(":", 1)[1].strip()
            add_client(addr, username)
            print(f"[SERVER] {username} joined from {addr}")
            # confirm join
            sock.sendto(f"broadcast:welcome {username}".encode(ENCODING), addr)
            continue

        if raw.startswith("answer:"):
            ans = raw.split(":", 1)[1].strip().upper()
            username = usernames.get(addr, f"{addr[0]}:{addr[1]}")
            received_answers.append((username, ans))
            print(f"[SERVER] {username} answered {ans}")

            # only first CORRECT gets point
            if winner is None and ans == correct:
                winner = username
                scores[username] = scores.get(username, 0) + 1

    # after timeout: reveal
    if winner:
        result_msg = f"broadcast:TIMEUP Correct={correct} Winner={winner}"
        print(f"[SERVER] Winner={winner}, correct={correct}")
    else:
        result_msg = f"broadcast:TIMEUP Correct={correct} Winner=None"
        print(f"[SERVER] No correct answer. Correct={correct}")

    broadcast(result_msg)
    # also broadcast leaderboard
    lb = leaderboard_text()
    broadcast(lb)
    print("[SERVER] Leaderboard =>", lb)


def main():
    print("[SERVER] UDP quiz server running on port", SERVER_PORT)
    print("[SERVER] Waiting for players to send: join:<username>")

    # wait a bit before starting first round so clients can join
    warmup_deadline = time.time() + 30
    while time.time() < warmup_deadline:
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

    print("[SERVER] Starting quiz!")
    broadcast("broadcast:QUIZ_START")

    for q in QUESTIONS:
        ask_question(q)
        time.sleep(2)  # small pause between questions

    broadcast("broadcast:QUIZ_END")
    final_lb = leaderboard_text()
    broadcast(final_lb)
    print("[SERVER] Quiz finished.")
    print("[SERVER] Final leaderboard:", final_lb)


if __name__ == "__main__":
    main()
