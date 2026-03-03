import string
import random
import uuid
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, Optional
import uvicorn

from game import Room, Player, GameState, Play, DiceFace, PlayType

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {} # room_id -> {player_id: ws}
        self.rooms: Dict[str, Room] = {}

    async def connect(self, ws: WebSocket, room_id: str, player_id: str):
        await ws.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = {}
        self.active_connections[room_id][player_id] = ws

    def disconnect(self, room_id: str, player_id: str):
        if room_id in self.active_connections and player_id in self.active_connections[room_id]:
            del self.active_connections[room_id][player_id]

    async def broadcast_room_state(self, room_id: str):
        if room_id not in self.rooms: return
        room = self.rooms[room_id]
        
        base_state = room.model_dump()
        
        for p in room.players:
            if room_id in self.active_connections and p.id in self.active_connections[room_id]:
                ws = self.active_connections[room_id][p.id]
                player_state = base_state.copy()
                hide_real = True
                
                if room.state in [GameState.KIRIKI_PUNISHMENT, GameState.GAME_OVER, GameState.REVEALING]:
                    hide_real = False
                elif room.state == GameState.PLAYING_DECLARE and p.id == room.current_player.id:
                    hide_real = False
                elif room.state == GameState.PLAYING_REACT and p.id == room.last_player_id:
                    hide_real = False
                elif room.state == GameState.PLAYING_ROLL and room.last_player_id and p.id == room.last_player_id:
                    # Even immediately after react_believe, the previous player might still know their real roll conceptually, 
                    # but actually we probably want to keep it hidden now.
                    hide_real = True

                if hide_real:
                    player_state["current_real_play"] = None

                msg = {
                    "type": "room_state",
                    "state": player_state,
                    "me": p.id
                }
                
                try:
                    await ws.send_json(msg)
                except Exception:
                    pass

manager = ConnectionManager()

def generate_room_id() -> str:
    return ''.join(random.choices(string.ascii_uppercase, k=4))

class CreateRoomRequest(BaseModel):
    player_name: str

class JoinRoomRequest(BaseModel):
    room_id: str
    player_name: str

@app.post("/api/rooms")
async def create_room(req: CreateRoomRequest):
    room_id = generate_room_id()
    while room_id in manager.rooms:
        room_id = generate_room_id()
        
    room = Room(id=room_id)
    player_id = str(uuid.uuid4())
    room.add_player(player_id, req.player_name)
    manager.rooms[room_id] = room
    
    return {"room_id": room_id, "player_id": player_id}

@app.post("/api/rooms/{room_id}/join")
async def join_room(room_id: str, req: JoinRoomRequest):
    room_id = room_id.upper()
    if room_id not in manager.rooms:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Room not found")
        
    room = manager.rooms[room_id]
    if room.state != GameState.WAITING_FOR_PLAYERS:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Game already started")
        
    player_id = str(uuid.uuid4())
    room.add_player(player_id, req.player_name)
    
    asyncio.create_task(manager.broadcast_room_state(room_id))
    return {"player_id": player_id}

@app.websocket("/ws/{room_id}/{player_id}")
async def websocket_endpoint(ws: WebSocket, room_id: str, player_id: str):
    room_id = room_id.upper()
    if room_id not in manager.rooms:
        await ws.close()
        return
        
    room = manager.rooms[room_id]
    player = room.get_player(player_id)
    if not player:
        await ws.close()
        return

    await manager.connect(ws, room_id, player_id)
    player.connected = True
    await manager.broadcast_room_state(room_id)

    try:
        while True:
            data = await ws.receive_json()
            action = data.get("action")
            
            if action == "start_game":
                if room.state == GameState.WAITING_FOR_PLAYERS and len(room.players) >= 2:
                    room.state = GameState.PLAYING_ROLL
                    room.current_turn_index = 0
            
            elif action == "roll_dice":
                if room.state == GameState.PLAYING_ROLL and room.current_player.id == player_id:
                    room.current_real_play = Play.roll()
                    room.state = GameState.PLAYING_DECLARE
                    
            elif action == "declare":
                if room.state == GameState.PLAYING_DECLARE and room.current_player.id == player_id:
                    is_true = data.get("is_true", True)
                    
                    if is_true:
                        new_declare = room.current_real_play
                    else:
                        d1 = DiceFace(data["dice1"])
                        d2 = DiceFace(data["dice2"])
                        new_declare = Play(dice1=d1, dice2=d2)
                        
                    if room.last_declared_play and not new_declare.is_greater_or_equal_to(room.last_declared_play):
                        # Block invalid declare, cannot declare something smaller or equal
                        continue
                        
                    if is_true and room.last_declared_play and not room.current_real_play.is_greater_or_equal_to(room.last_declared_play):
                        # Block "Truth" button exploits if the real roll is ACTUALLY smaller or equal
                        continue
                        
                    room.current_declared_play = new_declare
                    room.last_declared_play = new_declare
                        
                    if room.current_declared_play.play_type == PlayType.KIRIKI:
                        room.state = GameState.KIRIKI_PUNISHMENT
                    else:
                        room.last_player_id = player_id
                        room.update_turn()
                        room.state = GameState.PLAYING_REACT
            
            elif action == "kiriki_target":
                if room.state == GameState.KIRIKI_PUNISHMENT and room.kiriki_target_id is None and room.current_player.id == player_id:
                    target_id = data.get("target_id")
                    if target_id and room.get_player(target_id):
                        room.kiriki_target_id = target_id
                        room.kiriki_attacker_id = player_id
                        room.kiriki_attempts_left = 3
                        room.kiriki_pairs_rolled = 0
                        room.update_turn(target_id)
            
            elif action == "kiriki_defense_roll":
                if room.state == GameState.KIRIKI_PUNISHMENT and room.kiriki_target_id == player_id:
                    if room.kiriki_attempts_left > 0:
                        room.kiriki_attempts_left -= 1
                        roll = Play.roll()
                        room.current_real_play = roll
                        
                        success = False
                        if roll.play_type == PlayType.KIRIKI:
                            success = True
                        elif roll.play_type == PlayType.PAIR:
                            room.kiriki_pairs_rolled += 1
                            if room.kiriki_pairs_rolled >= 2:
                                success = True
                                
                        if success:
                            room.kiriki_target_id = room.kiriki_attacker_id
                            room.kiriki_attacker_id = player_id
                            room.kiriki_attempts_left = 3
                            room.kiriki_pairs_rolled = 0
                            room.update_turn(room.kiriki_target_id)
                        else:
                            if room.kiriki_attempts_left == 0:
                                loser = room.get_player(player_id)
                                loser.lives -= 1
                                room.reset_round(loser_id=loser.id)

            elif action == "react_believe":
                if room.state == GameState.PLAYING_REACT and room.current_player.id == player_id:
                    # Player believed the claim. That claim becomes the new minimum to beat.
                    # last_declared_play is already set to current_declared_play.
                    room.state = GameState.PLAYING_ROLL
            
            elif action == "react_lift":
                if room.state == GameState.PLAYING_REACT and room.current_player.id == player_id:
                    real = room.current_real_play
                    decl = room.current_declared_play
                    
                    # You only lie if real doesn't match decl, OR if real doesn't meet the target logic
                    if real and (real.play_type == decl.play_type and real.value1 == decl.value1 and real.value2 == decl.value2):
                        # It is EXACTLY the same play. Truth.
                        liar_id = player_id # The lifter was wrong
                        next_turn_id = None # Skip their turn, calculate next valid
                        skip_turn = True
                    elif real and decl.is_greater_or_equal_to(real) and not (decl.play_type == real.play_type and decl.value1 == real.value1 and decl.value2 == real.value2):
                        # The claim was bigger than reality. Lie.
                        liar_id = room.last_player_id # The declarer lied
                        next_turn_id = player_id # The lifter gets the turn
                        skip_turn = False
                    else:
                        # Reality was actually > claim (Under-claiming). This is Truth.
                        liar_id = player_id
                        next_turn_id = None
                        skip_turn = True
                        
                    room.state = GameState.REVEALING
                    await manager.broadcast_room_state(room_id)
                    
                    # 1. Suspense Reveal Phase
                    await asyncio.sleep(4)
                    
                    # 2. Determine loser and show who lost Life
                    loser_player = room.get_player(liar_id)
                    if loser_player:
                        loser_player.lives -= 1
                    
                    # Transition to LIFE_LOST state to show the penalty
                    room.state = GameState.LIFE_LOST
                    # We store the loser id temporarily in last_player_id to display it on frontend
                    room.last_player_id = liar_id 
                    await manager.broadcast_room_state(room_id)
                    
                    # 3. Life Lost Display Phase
                    await asyncio.sleep(3)
                    
                    # 4. Check for Game Over or Reset Round
                    active_players = [p for p in room.players if p.lives > 0]
                    if len(active_players) <= 1:
                        room.state = GameState.GAME_OVER
                    else:
                        room.reset_round(loser_id=(next_turn_id if next_turn_id else liar_id), skip_turn=skip_turn)
                        
                    await manager.broadcast_room_state(room_id)
                    continue
                    
            elif action == "leave_room":
                room.remove_player(player_id)
                await manager.broadcast_room_state(room_id)
                # Client will close on their end, but we can return here
                return
            
            await manager.broadcast_room_state(room_id)
            
    except WebSocketDisconnect:
        manager.disconnect(room_id, player_id)
        room = manager.rooms.get(room_id)
        if room:
            room.remove_player(player_id)
            if not room.players:
                del manager.rooms[room_id]
            else:
                await manager.broadcast_room_state(room_id)
