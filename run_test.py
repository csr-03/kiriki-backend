import sys
sys.path.append('.')

import asyncio
import uuid
import json
from main import app, manager
from game import Room, Player, Play, DiceFace, GameState
from fastapi.testclient import TestClient

client = TestClient(app)

def test_websocket():
    # 1. Create room
    res = client.post("/api/rooms", json={"player_name": "Test1"})
    data = res.json()
    room_id = data["room_id"]
    p1_id = data["player_id"]
    
    # Add a second player manually
    room = manager.rooms[room_id]
    p2_id = str(uuid.uuid4())
    room.add_player(p2_id, "Test2")
    
    with client.websocket_connect(f"/ws/{room_id}/{p1_id}") as websocket1:
        with client.websocket_connect(f"/ws/{room_id}/{p2_id}") as websocket2:
            websocket1.send_json({"action": "start_game"})
            
            # Flush start initial states
            print("p1 gets after start:", websocket1.receive_json()["state"]) 
            print("p1 current_turn_index:", websocket1.receive_json()["current_turn_index"]) 
            
            # Roll dice P1
            websocket1.send_json({"action": "roll_dice"})
            
            # Read until PLAYING_DECLARE
            state1 = None
            while True:
                resp = websocket1.receive_json()
                if resp.get("state") == GameState.PLAYING_DECLARE:
                    state1 = resp
                    break
            
            print("P1 Rolled. State is:", state1["state"])
            play = state1["current_real_play"]
            print("P1 Real dice:", play)
            
            # Declare Truth
            websocket1.send_json({"action": "declare", "is_true": True})
            try:
                # Read until state changes or we timeout (testclient doesn't really timeout easily but we'll try)
                print("Waiting for response to Declare Truth...")
                resp = websocket1.receive_json()
                print("Response State:", resp["state"])
            except Exception as e:
                print("Exception caught when waiting for declare response:", e)

if __name__ == "__main__":
    test_websocket()
