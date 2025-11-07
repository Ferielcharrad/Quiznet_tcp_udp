"""
TCP quiz game client for the Kahoot-style Transport Layer Quiz.

Features:
- Connect to TCP server with username
- Receive and display questions in real-time
- Submit answers (A/B/C/D)
- See live timer countdown
- View results with points earned
- Display leaderboard after each question
- Rich terminal-based UI with colors

Usage:
    python client_tcp.py
    
    Then enter:
    - Server IP (or press Enter for localhost)
    - Username
    - Answer questions with A, B, C, or D
"""

import socket
import threading
import time
import sys
import os

# Try to import colorama for colored output (optional)
try:
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False
    # Fallback: define empty color codes
    class Fore:
        RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = RESET = ""
    class Back:
        RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = BLACK = RESET = ""
    class Style:
        BRIGHT = DIM = NORMAL = RESET_ALL = ""

# Server connection settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8888
ENCODING = "utf-8"

# Client state
sock = None
running = True
current_question = None
current_question_id = None
question_timeout = 15
timer_value = 0
answered = False
waiting_for_results = False
my_username = ""


def clear_screen():
    """Clear the terminal screen."""
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header():
    """Print the game header."""
    print(Fore.CYAN + Style.BRIGHT + "╔════════════════════════════════════════════════════════╗")
    print(Fore.CYAN + Style.BRIGHT + "║                                                        ║")
    print(Fore.CYAN + Style.BRIGHT + "║        🎮 QUIZNET - KAHOOT-STYLE TCP CLIENT 🎮        ║")
    print(Fore.CYAN + Style.BRIGHT + "║                                                        ║")
    print(Fore.CYAN + Style.BRIGHT + "║           Transport Layer Quiz Competition            ║")
    print(Fore.CYAN + Style.BRIGHT + "║                                                        ║")
    print(Fore.CYAN + Style.BRIGHT + "╚════════════════════════════════════════════════════════╝")
    print()


def print_separator(char="═", length=60, color=Fore.CYAN):
    """Print a separator line."""
    print(color + char * length)


def print_question_box(question_text, question_num, timeout):
    """Display the current question in a nice box."""
    print()
    print_separator("━", 60, Fore.YELLOW)
    print(Fore.YELLOW + Style.BRIGHT + f"📝 QUESTION {question_num}")
    print_separator("━", 60, Fore.YELLOW)
    print()
    print(Fore.WHITE + Style.BRIGHT + question_text)
    print()
    print(Fore.CYAN + f"⏱️  Time limit: {timeout} seconds")
    print()


def print_timer(seconds_left):
    """Print the countdown timer."""
    if seconds_left <= 3:
        color = Fore.RED
        icon = "⚠️ "
    elif seconds_left <= 7:
        color = Fore.YELLOW
        icon = "⏰ "
    else:
        color = Fore.GREEN
        icon = "⏱️  "
    
    # Create a progress bar
    total_bars = 30
    filled = int((seconds_left / question_timeout) * total_bars)
    bar = "█" * filled + "░" * (total_bars - filled)
    
    print(f"\r{color}{icon}Time: {seconds_left:2d}s [{bar}] ", end="", flush=True)


def print_results(winner, correct_answer, points=None):
    """Display the results after a question."""
    print("\n")
    print_separator("═", 60, Fore.MAGENTA)
    print(Fore.MAGENTA + Style.BRIGHT + "📊 RESULTS")
    print_separator("═", 60, Fore.MAGENTA)
    print()
    
    print(Fore.CYAN + Style.BRIGHT + f"✅ Correct Answer: {correct_answer}")
    print()
    
    if winner and winner != "None":
        print(Fore.YELLOW + Style.BRIGHT + f"🏆 Winner: {winner}")
        if points:
            print(Fore.GREEN + Style.BRIGHT + f"   Points: {points}")
    else:
        print(Fore.YELLOW + "No one answered correctly")
    print()


def print_feedback(result, points, time_taken):
    """Display personal feedback."""
    if result == "correct":
        print(Fore.GREEN + Style.BRIGHT + "═" * 60)
        print(Fore.GREEN + Style.BRIGHT + f"✅ CORRECT! You earned {points} points!")
        print(Fore.GREEN + f"⚡ Answer time: {time_taken}s")
        print(Fore.GREEN + Style.BRIGHT + "═" * 60)
    elif result == "wrong":
        print(Fore.RED + Style.BRIGHT + "═" * 60)
        print(Fore.RED + Style.BRIGHT + "❌ WRONG! Better luck next time!")
        print(Fore.RED + Style.BRIGHT + "═" * 60)
    elif result == "timeout":
        print(Fore.YELLOW + Style.BRIGHT + "═" * 60)
        print(Fore.YELLOW + Style.BRIGHT + "⏰ TIME'S UP! You didn't answer in time.")
        print(Fore.YELLOW + Style.BRIGHT + "═" * 60)
    print()


def print_leaderboard(scores_data):
    """Display the leaderboard."""
    print()
    print_separator("═", 60, Fore.CYAN)
    print(Fore.CYAN + Style.BRIGHT + "🏆 LEADERBOARD")
    print_separator("═", 60, Fore.CYAN)
    print()
    
    if scores_data == "EMPTY:0" or not scores_data:
        print(Fore.YELLOW + "No scores yet.")
    else:
        # Parse scores: "user1:100|user2:50|user3:25"
        entries = scores_data.split("|")
        for rank, entry in enumerate(entries, 1):
            if ":" in entry:
                username, points = entry.split(":", 1)
                
                # Highlight current user
                if username == my_username:
                    color = Fore.GREEN + Style.BRIGHT
                    prefix = "➤ "
                else:
                    color = Fore.WHITE
                    prefix = "  "
                
                # Add medals for top 3
                if rank == 1:
                    medal = "🥇"
                elif rank == 2:
                    medal = "🥈"
                elif rank == 3:
                    medal = "🥉"
                else:
                    medal = f"{rank}."
                
                print(f"{prefix}{color}{medal} {username}: {points} pts")
    
    print()
    print_separator("═", 60, Fore.CYAN)


def send_message(message: str) -> bool:
    """Send a message to the server."""
    try:
        sock.sendall((message + "\n").encode(ENCODING))
        return True
    except OSError as e:
        print(Fore.RED + f"\n[ERROR] Failed to send message: {e}")
        return False


def receive_loop():
    """
    Background thread that receives messages from the server.
    
    Message types:
    - broadcast:<message> - General broadcast message
    - question:<id>:<timeout>:<text> - New question
    - timer:<seconds> - Countdown timer update
    - show:results - Show results page
    - show:leaderboard - Show leaderboard page
    - feedback:<username>:<result>:<points>:<time> - Personal feedback
    - score:<leaderboard_data> - Leaderboard data
    - error:<message> - Error message
    """
    global running, current_question, current_question_id, question_timeout
    global timer_value, answered, waiting_for_results, sock
    
    buffer = ""
    
    while running:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                print(Fore.RED + "\n[ERROR] Connection closed by server.")
                running = False
                break
            
            buffer += chunk.decode(ENCODING, errors="ignore")
            
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                
                if not line:
                    continue
                
                # Parse different message types
                if line.startswith("broadcast:"):
                    msg = line.split(":", 1)[1]
                    print(Fore.CYAN + Style.BRIGHT + f"\n📢 {msg}")
                    
                    # Special handling for specific broadcasts
                    if "TIMEUP" in msg or "Winner=" in msg:
                        # Parse: "TIMEUP Correct=A Winner=user1 Points=950"
                        parts = msg.split()
                        correct = winner = points = None
                        
                        for part in parts:
                            if part.startswith("Correct="):
                                correct = part.split("=")[1]
                            elif part.startswith("Winner="):
                                winner = part.split("=")[1]
                            elif part.startswith("Points="):
                                points = part.split("=")[1]
                        
                        if correct:
                            print_results(winner, correct, points)
                    
                    elif "QUIZ_START" in msg:
                        clear_screen()
                        print_header()
                        print(Fore.GREEN + Style.BRIGHT + "🚀 QUIZ IS STARTING!")
                        print()
                    
                    elif "QUIZ_END" in msg:
                        print()
                        print(Fore.CYAN + Style.BRIGHT + "🎉 QUIZ COMPLETED!")
                        print()
                
                elif line.startswith("question:"):
                    # Format: "question:<id>:<timeout>:<text>"
                    parts = line.split(":", 3)
                    if len(parts) >= 4:
                        question_id = parts[1]
                        timeout = int(parts[2])
                        question_text = parts[3]
                        
                        current_question_id = question_id
                        current_question = question_text
                        question_timeout = timeout
                        answered = False
                        waiting_for_results = False
                        
                        # Clear screen and display question
                        clear_screen()
                        print_header()
                        print_question_box(question_text, question_id, timeout)
                        print(Fore.GREEN + "Type your answer (A, B, C, or D) and press Enter:")
                        print()
                
                elif line.startswith("timer:"):
                    # Format: "timer:<seconds_remaining>"
                    seconds = int(line.split(":", 1)[1])
                    timer_value = seconds
                    
                    # Only show timer if we're in a question and haven't answered
                    if current_question and not answered and not waiting_for_results:
                        print_timer(seconds)
                
                elif line.startswith("show:results"):
                    waiting_for_results = True
                    print("\n")
                
                elif line.startswith("show:leaderboard"):
                    # Next message will be the leaderboard
                    pass
                
                elif line.startswith("feedback:"):
                    # Format: "feedback:<username>:<result>:<points>:<time>"
                    parts = line.split(":", 4)
                    if len(parts) >= 5:
                        username = parts[1]
                        result = parts[2]
                        points = parts[3]
                        time_taken = parts[4]
                        
                        if username == my_username:
                            print_feedback(result, points, time_taken)
                
                elif line.startswith("score:"):
                    # Format: "score:<leaderboard_data>"
                    scores_data = line.split(":", 1)[1]
                    print_leaderboard(scores_data)
                
                elif line.startswith("error:"):
                    error_msg = line.split(":", 1)[1]
                    print(Fore.RED + Style.BRIGHT + f"\n❌ ERROR: {error_msg}")
                    
                    if "username_taken" in error_msg or "ip_exists" in error_msg:
                        print(Fore.YELLOW + "Please restart and choose a different username.")
                        running = False
        
        except OSError:
            if running:
                print(Fore.RED + "\n[ERROR] Connection lost.")
            running = False
            break
        except Exception as e:
            print(Fore.RED + f"\n[ERROR] Unexpected error: {e}")
            running = False
            break


def input_loop():
    """
    Main thread that handles user input for answers.
    """
    global running, answered, current_question
    
    while running:
        try:
            if current_question and not answered and not waiting_for_results:
                user_input = input().strip().upper()
                
                if user_input in ("A", "B", "C", "D"):
                    if send_message(f"answer:{user_input}"):
                        answered = True
                        print(Fore.GREEN + f"\n✅ Answer '{user_input}' submitted!")
                        print(Fore.YELLOW + "⏳ Waiting for results...")
                        print()
                elif user_input:
                    print(Fore.RED + "Invalid input! Please enter A, B, C, or D.")
            else:
                time.sleep(0.1)
        
        except EOFError:
            break
        except KeyboardInterrupt:
            print(Fore.YELLOW + "\n\n👋 Disconnecting...")
            running = False
            break


def connect_to_server(host: str, port: int, username: str) -> bool:
    """
    Connect to the quiz server and send join request.
    
    Returns True if successful, False otherwise.
    """
    global sock, my_username
    
    try:
        print(Fore.CYAN + f"Connecting to {host}:{port}...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)  # 10 second timeout for connection
        sock.connect((host, port))
        sock.settimeout(None)  # Remove timeout after connection
        
        print(Fore.GREEN + "✅ Connected!")
        print()
        
        # Send join request
        my_username = username
        if not send_message(f"join:{username}"):
            return False
        
        print(Fore.CYAN + f"Joined as: {Fore.GREEN + Style.BRIGHT}{username}")
        print(Fore.YELLOW + "Waiting for the quiz to start...")
        print()
        print_separator()
        print()
        
        return True
    
    except socket.timeout:
        print(Fore.RED + "❌ Connection timeout. Server may be unreachable.")
        return False
    except ConnectionRefusedError:
        print(Fore.RED + "❌ Connection refused. Is the server running?")
        return False
    except OSError as e:
        print(Fore.RED + f"❌ Connection error: {e}")
        return False


def main():
    """
    Main entry point for the TCP quiz client.
    """
    global running, sock
    
    clear_screen()
    print_header()
    
    # Get server address
    print(Fore.CYAN + "Enter server details:")
    print_separator("-", 60, Fore.CYAN)
    
    host_input = input(f"Server IP (press Enter for {DEFAULT_HOST}): ").strip()
    host = host_input if host_input else DEFAULT_HOST
    
    port_input = input(f"Server Port (press Enter for {DEFAULT_PORT}): ").strip()
    if port_input:
        try:
            port = int(port_input)
        except ValueError:
            print(Fore.RED + "Invalid port number. Using default.")
            port = DEFAULT_PORT
    else:
        port = DEFAULT_PORT
    
    # Get username
    username = ""
    while not username:
        username = input("\nEnter your username: ").strip()
        if not username:
            print(Fore.RED + "Username cannot be empty!")
    
    print()
    print_separator()
    print()
    
    # Connect to server
    if not connect_to_server(host, port, username):
        print(Fore.RED + "\nFailed to connect to server. Exiting...")
        return
    
    # Start receiver thread
    receiver_thread = threading.Thread(target=receive_loop, daemon=True)
    receiver_thread.start()
    
    # Run input loop in main thread
    try:
        input_loop()
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\n\n👋 Disconnecting...")
    finally:
        running = False
        if sock:
            try:
                sock.close()
            except:
                pass
        
        print()
        print(Fore.CYAN + "═" * 60)
        print(Fore.CYAN + "Thanks for playing! 👋")
        print(Fore.CYAN + "═" * 60)
        print()


if __name__ == "__main__":
    main()
