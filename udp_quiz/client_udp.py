# client_udp.py
import socket
import threading
import sys

ENCODING = "utf-8"
SERVER_PORT = 8888

# Shared state
current_question_id = None
current_question_text = None
lock = threading.Lock()

def listener(sock: socket.socket):
    """
    Background thread:
    receive messages from server and print them nicely.
    Also updates the current question.
    """
    global current_question_id, current_question_text

    while True:
        data, addr = sock.recvfrom(4096)
        msg = data.decode(ENCODING, errors="ignore").strip()

        if msg.startswith("question:"):
            # Format: question:<id>:<text>
            parts = msg.split(":", 2)
            if len(parts) >= 3:
                qid = parts[1]
                qtext = parts[2]
                with lock:
                    current_question_id = qid
                    current_question_text = qtext
                print("\n------------------------------")
                print(f"[QUESTION {qid}] {qtext}")
                print("Type your answer letter (A/B/C/...) and press Enter.")
                print("------------------------------")

        elif msg.startswith("broadcast:"):
            # broadcast:<message>
            text = msg.split(":", 1)[1]
            print(f"[SERVER BROADCAST] {text}")

        elif msg.startswith("score:"):
            # score:user1:3|user2:1|...
            raw_lb = msg.split(":", 1)[1] if ":" in msg else ""
            if raw_lb == "EMPTY:0":
                print("[SCOREBOARD] no scores yet")
            else:
                print("\n===== SCOREBOARD =====")
                players = raw_lb.split("|")
                rank = 1
                for p in players:
                    # each p like "username:points"
                    if ":" in p:
                        uname, pts = p.split(":", 1)
                        print(f"{rank}. {uname} -> {pts} pts")
                        rank += 1
                print("======================\n")

        else:
            # fallback raw print
            print(f"[SERVER MSG] {msg}")


def main():
    if len(sys.argv) < 3:
        print("Usage: python client_udp.py <server_ip> <username>")
        sys.exit(1)

    server_ip = sys.argv[1]
    username = sys.argv[2]

    # create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(True)

    server_addr = (server_ip, SERVER_PORT)

    # send join
    join_msg = f"join:{username}".encode(ENCODING)
    sock.sendto(join_msg, server_addr)
    print(f"[CLIENT] Sent join request as '{username}' to {server_ip}:{SERVER_PORT}")

    # start listener thread
    t = threading.Thread(target=listener, args=(sock,), daemon=True)
    t.start()

    # main loop: read user input for answers
    # whenever you type something and press Enter, we send answer:<something>
    while True:
        user_input = input().strip()
        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            print("[CLIENT] Bye.")
            break

        # send answer
        ans_msg = f"answer:{user_input}".encode(ENCODING)
        sock.sendto(ans_msg, server_addr)
        print(f"[CLIENT] Sent answer '{user_input}'")

    sock.close()


if __name__ == "__main__":
    main()
