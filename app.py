from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import pymongo
import uuid
import faiss
from datetime import datetime
import ollama
import textwrap
import os
from langchain_ollama import OllamaEmbeddings
import numpy as np

app = Flask(__name__)
app.secret_key = "supersecretkey"  # Change this in production
bcrypt = Bcrypt(app)

# Database Connection
mongo_client = pymongo.MongoClient("mongodb://localhost:27017/")
db = mongo_client["mental_health_chatbot"]
users_collection = db["users"]
chat_collection = db["documents"]

# Ensure directories exist
FAISS_DIR = "faiss_indexes"
MSG_DIR = "messages"
os.makedirs(FAISS_DIR, exist_ok=True)
os.makedirs(MSG_DIR, exist_ok=True)

# ---------------------------
# FAISS and Message Storage
# ---------------------------
EMBEDDING_DIM = 768
embeddings = OllamaEmbeddings(model="nomic-embed-text")

def get_user_files():
    """Generate paths for user-specific FAISS index and text file."""
    user_id = current_user.id
    return f"{FAISS_DIR}/faiss_index_{user_id}.index", f"{MSG_DIR}/message_store_{user_id}.txt"

def load_faiss_store():
    """Load FAISS index for the logged-in user."""
    faiss_index = faiss.IndexFlatL2(EMBEDDING_DIM)
    index_file, _ = get_user_files()
    if index_file and os.path.exists(index_file):
        faiss_index = faiss.read_index(index_file)
    return faiss_index

def save_faiss_store(faiss_index):
    """Save FAISS index for the logged-in user."""
    index_file, _ = get_user_files()
    if index_file:
        faiss.write_index(faiss_index, index_file)

def store_vector_message(text, role, faiss_index):
    """Store messages in FAISS and user-specific text file."""
    _, text_file = get_user_files()
    if not text_file:
        return

    clean_text = " ".join(text.split())
    embedding = embeddings.embed_documents([clean_text])[0]
    faiss_index.add(np.array([embedding], dtype=np.float32))

    with open(text_file, "a", encoding="utf-8") as f:
        f.write(f"{role}: {clean_text}\n")

def retrieve_context(query, faiss_index, top_k=3):
    """Retrieve most relevant messages for a given query."""
    _, text_file = get_user_files()
    if not text_file or not os.path.exists(text_file):
        return ""

    with open(text_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if not lines:
        return ""

    query_embedding = embeddings.embed_query(query)
    distances, indices = faiss_index.search(np.array([query_embedding], dtype=np.float32), top_k)
    
    retrieved_texts = [lines[i].strip().split(":", 1)[1] for i in indices[0] if 0 <= i < len(lines)]
    return "\n".join(retrieved_texts)

def get_last_messages(num_lines=4):
    """Retrieve the last `num_lines` of messages from the current session's message text file."""
    _, text_file = get_user_files()
    if not text_file or not os.path.exists(text_file):
        return ""
    with open(text_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    # Return the last num_lines (or all if there are less than num_lines)
    return "\n".join([line.strip() for line in lines[-num_lines:]])

# ---------------------------
# Flask-Login Configuration
# ---------------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# User Class for Authentication
class User(UserMixin):
    def __init__(self, user_id, username):
        self.id = user_id
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    user = users_collection.find_one({"_id": user_id})
    if user:
        return User(user["_id"], user["username"])
    return None

# Home Route
@app.route("/")
@login_required
def index():
    return render_template("index.html", username=current_user.username)

# Signup Route
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if users_collection.find_one({"username": username}):
            flash("Username already exists. Please log in.")
            return redirect(url_for("login"))

        hashed_pw = bcrypt.generate_password_hash(password).decode("utf-8")
        user_id = str(uuid.uuid4())
        users_collection.insert_one({"_id": user_id, "username": username, "password": hashed_pw})

        flash("Signup successful! Please log in.")
        return redirect(url_for("login"))

    return render_template("signup.html")

# Login Route
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = users_collection.find_one({"username": username})

        if user and bcrypt.check_password_hash(user["password"], password):
            login_user(User(user["_id"], username))
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials. Please try again.")
    
    return render_template("login.html")

# Logout Route
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# Fetch Chat History (Only for Logged-in User)
@app.route("/history")
@login_required
def chat_history():
    user_id = current_user.id
    history = {}
    chats = chat_collection.find({"user_id": user_id})

    for chat in chats:
        date = chat["timestamp"].strftime("%Y-%m-%d")
        time = chat["timestamp"].strftime("%H:%M")
        if date not in history:
            history[date] = []
        history[date].append({
            "time": time,
            "user_message": chat["user_message"],
            "bot_response": chat["bot_response"]
        })

    return jsonify(history)

# Handle Chat Messages
@app.route("/chat", methods=["POST"])
@login_required
def chat():
    data = request.get_json()
    user_input = data["message"]

    # Load FAISS index and generate bot response using additional context
    faiss_index = load_faiss_store()
    bot_response = generate_response(user_input, faiss_index)

    # Store chat
    chat_collection.insert_one({
        "user_id": current_user.id,
        "user_message": user_input,
        "bot_response": bot_response,
        "timestamp": datetime.now()
    })
    store_vector_message(user_input, "user", faiss_index)
    store_vector_message(bot_response, "bot", faiss_index)
    save_faiss_store(faiss_index)

    return jsonify({"bot_response": bot_response})

# ---------------------------
# Chatbot and Crisis Handling
# ---------------------------
SUPPORTIVE_PROMPT = """
You are a compassionate mental health assistant. Your goal is to provide emotional support, mindfulness tips,
and mental well-being advice. Always respond with kindness and encouragement.
If a user expresses severe distress (e.g., hopelessness, self-harm thoughts), recommend professional help instead of generic responses.
answer in 50 words or less unless asked.
"""

# Crisis keywords for emergency response
CRISIS_KEYWORDS = ["suicide", "hopeless", "self-harm", "end my life", "no reason to live", "can't go on"]

def format_text(text, width=80):
    """Format text for better readability in the terminal."""
    return "\n".join(textwrap.wrap(text, width))

def detect_crisis(message):
    """Check if user message contains crisis-related words."""
    return any(word in message.lower() for word in CRISIS_KEYWORDS)

# ---------------------------
# Chatbot Response Generation
# ---------------------------
def generate_response(user_input, faiss_index):
    """Generate chatbot response with relevant context."""
    if detect_crisis(user_input):
        return (
            "💙 I'm really sorry you're feeling this way. You're not alone.\n"
            "📞 Please reach out to a trusted friend, family member, or professional.\n"
            "🆘 Find a mental health helpline here: https://findahelpline.com/"
        )

    # Retrieve relevant context using FAISS
    retrieved_context = retrieve_context(user_input, faiss_index, top_k=2)
    # Retrieve the last 4 messages from the session's message text file
    last_context = get_last_messages(10)
    print(last_context)

    # Combine both contexts as additional context for the bot
    combined_context = ""
    if retrieved_context:
        combined_context += f"Relevant context:\n{retrieved_context}\n\n"
    if last_context:
        combined_context += f"Recent messages:\n{last_context}\n\n"
    
    prompt_body = f"{combined_context}User: {user_input}\nChatbot:"
    model_response = ollama.chat(model="echo-care", messages=[
        {"role": "system", "content": SUPPORTIVE_PROMPT},
        {"role": "user", "content": prompt_body}
    ])
    return model_response['message']['content']

if __name__ == "__main__":
    app.run(debug=True)
