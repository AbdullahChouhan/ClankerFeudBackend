import socketio
import asyncio
from aiohttp import web
from pydantic import SecretStr
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.output_parsers import JsonOutputParser
from trivia_manager import TriviaManager

# --- 1. INITIALIZE TRIVIA & STATE ---
trivia_manager = TriviaManager("trivia_data")

# --- 2. SET UP THE AI BRAIN & PROMPTS ---
llm = ChatOpenAI(
    base_url="http://localhost:5001/v1", 
    api_key=SecretStr("sk-no-key-needed"), 
    model="koboldcpp",
    temperature=0.7,
    max_completion_tokens=60
)

base_persona = "You are Clanker, a whimsical but highly sarcastic robot game show host. Keep your responses strictly under 2 sentences."

# Prompt A: Reacting to physics
event_prompt = ChatPromptTemplate.from_messages([
    ("system", f"{base_persona} Mock the player's physical performance slightly."),
    ("human", "Game Event: {game_event}")
])

# Prompt B: Asking the question
ask_prompt = ChatPromptTemplate.from_messages([
    ("system", f"{base_persona} You are about to ask a trivia question. Rephrase the provided question in your own snarky words. Do NOT reveal the answer!"),
    ("human", "Category: {category}\nRaw Question: {question}")
])

# Prompt C: Judging the answer (Updated for JSON output)
judge_prompt = ChatPromptTemplate.from_messages([
    ("system", base_persona + " Judge the player's answer against the True Answer. You MUST respond with ONLY valid JSON and no other markdown or text. Format: {{\"correct\": true_or_false, \"text\": \"your snarky 2-sentence response\"}}"),
    ("human", "Question: {question}\nTrue Answer: {correct_answer}\nPlayer Answered: {player_answer}")
])

# Create the 3 LCEL Chains
chain_event = event_prompt | llm | StrOutputParser()
chain_ask = ask_prompt | llm | StrOutputParser()
chain_judge = judge_prompt | llm | JsonOutputParser()

# --- 3. SET UP THE SERVER ---
sio = socketio.AsyncServer(async_mode='aiohttp', cors_allowed_origins='*')
app = web.Application()
sio.attach(app)

# --- GAME STATE MANAGEMENT ---
buzzer_locked = False
answering_player = None

# --- GAME STATE MANAGEMENT ---
MAX_PLAYERS = 4
players = {} 

# New Global Game Loop State
current_question = None  # Holds the trivia data for the current round
buzzer_locked = True     # Buzzers start LOCKED so people can't spam them between rounds
answering_player = None  # Tracks who won the race

@sio.event
async def connect(sid, environ):
    print(f"🔌 Client connected: {sid}")
    # We do NOT add them to the game yet. They are just at the menu.

@sio.event
async def disconnect(sid):
    print(f"❌ Client disconnected: {sid}")
    
    # Safely clean up memory if they were in the game
    if sid in players:
        del players[sid]
        # Notify remaining players to destroy this player's 3D model
        await sio.emit('player_left', {"player_id": sid})
        print(f"🧹 Cleaned up state for {sid}. Players remaining: {len(players)}")

# --- PHASE 5: THE LOBBY ---
@sio.event
async def join_lobby(sid, data):
    """Triggered when a player hits 'Connect' on the Unity UI"""
    
    if len(players) >= MAX_PLAYERS:
        await sio.emit('lobby_error', {"message": "Lobby is full!"}, to=sid)
        return

    # Initialize their state safely
    players[sid] = {
        "id": sid,
        "model_type": data.get("model_type", "dude"), # 'dude' or 'girl'
        "costume_id": data.get("costume_id", 0),      # 0, 1, 2, or 3 for Smash-style alts
        "x": 0.0, "y": 0.0, "z": 0.0,
        "rot_y": 0.0, 
        "score": 0
    }
    
    print(f"🎮 Player {sid} joined as {players[sid]['model_type']}.")

    # 1. Send the new player the FULL list of current players to spawn them
    await sio.emit('lobby_joined', {"me": sid, "players": list(players.values())}, to=sid)
    
    # 2. Tell everyone ELSE to spawn just this one new player
    await sio.emit('player_joined', players[sid], skip_sid=sid)

# --- REAL-TIME MOVEMENT SYNC ---
@sio.event
async def player_move(sid, data):
    # Safety check: ensure they are actually in the lobby
    if sid not in players: return

    # Update server authority state
    players[sid]['x'] = data['x']
    players[sid]['y'] = data['y']
    players[sid]['z'] = data['z']
    players[sid]['rot_y'] = data['rot_y']

    # BROADCAST TO OTHERS: 
    # 'skip_sid=sid' prevents sending the data back to the player who sent it,
    # reducing bandwidth and supporting your Unity Client-Side Prediction!
    update_data = {
        "player_id": sid,
        "x": data['x'], "y": data['y'], "z": data['z'],
        "rot_y": data['rot_y']
    }
    await sio.emit('state_update', update_data, skip_sid=sid)

# --- BUZZER LOGIC ---
@sio.event
async def hit_buzzer(sid):
    """Phase 2: The Race"""
    global buzzer_locked, answering_player, current_question
    
    # Reject the buzz if it's locked, or if no question is active
    if buzzer_locked or current_question is None:
        return

    print(f"🚨 Player {sid} slammed the buzzer first!")
    
    # LOCK the buzzers so no one else can steal it
    buzzer_locked = True
    answering_player = sid

    # Tell the winner to open their UI
    await sio.emit('buzzer_won', to=sid)

    # Tell everyone else they lost the race
    await sio.emit('buzzer_locked', skip_sid=sid)

# --- 4. ASYNC AI EXECUTORS ---

@sio.event
async def trigger_physics_event(sid, data):
    print(f"⚡ Physics Event: Offloading to background task...")
    async def task():
        response = await chain_event.ainvoke({"game_event": data['event_description']})
        await sio.emit('ai_commentary', {"text": str(response).strip()}, to=sid)
    asyncio.create_task(task())

@sio.event
async def request_question(sid):
    """Phase 1: Ask the question and unlock buzzers"""
    global current_question, buzzer_locked
    print(f"❓ Round started! Grabbing a question...")

    q_data = trivia_manager.get_random_question()
    
    if not q_data:
        await sio.emit('ai_commentary', {"text": "Error: No trivia loaded!"})
        return

    # Set the global question
    current_question = q_data 
    
    # UNLOCK THE BUZZERS for the race!
    buzzer_locked = False 

    async def task():
        response = await chain_ask.ainvoke({"category": q_data['category'], "question": q_data['question']})
        payload = {
            "text": str(response).strip(),
            "raw_category": q_data['category']
        }
        # BROADCAST TO EVERYONE
        await sio.emit('ai_ask_question', payload)
    
    asyncio.create_task(task())

@sio.event
async def submit_answer(sid, data):
    """Phase 3: The Verdict"""
    global current_question, answering_player

    # Reject if a rogue player tries to submit an answer
    if sid != answering_player or current_question is None:
        return

    print(f"🗣️ Player {sid} answered: {data['player_answer']}")

    async def task():
        global current_question, answering_player
        
        if current_question is None:
            await sio.emit('ai_commentary', {"correct": False, "text": "Error: No active question!", "scores": []}, to=sid)
            return

        # 1. Ask the LLM to judge the answer
        response = await chain_judge.ainvoke({
            "question": current_question['question'],
            "correct_answer": current_question['answer'],
            "player_answer": data['player_answer']
        })
        
        # 2. Award points if they were right!
        is_correct = response.get("correct", False)
        if is_correct and answering_player in players:
            players[answering_player]['score'] += 1
            print(f"💰 Awarded 1 point to {answering_player}!")

        # 3. Build the score payload to send to the Jumbotron
        current_scores = [{"id": p["id"], "score": p["score"]} for p in players.values()]
        
        # 4. Package it all together
        payload = {
            "correct": is_correct,
            "text": response.get("text", "Error parsing AI text."),
            "scores": current_scores
        }
        
        # 5. Broadcast the verdict and new scores to everyone
        await sio.emit('ai_commentary', payload)
        
        current_question = None
        answering_player = None

    asyncio.create_task(task())

if __name__ == '__main__':
    print("🚀 Server starting on http://localhost:5000")
    web.run_app(app, port=5000)