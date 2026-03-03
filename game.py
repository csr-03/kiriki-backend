import random
from typing import List, Optional, Dict
from pydantic import BaseModel
from enum import Enum

class DiceFace(str, Enum):
    BLACKS = "Negros"
    REDS = "Rojos"
    J = "J"
    Q = "Q"
    K = "K"
    ACE = "As"

DICE_VALUES = {
    DiceFace.BLACKS: 1,
    DiceFace.REDS: 2,
    DiceFace.J: 3,
    DiceFace.Q: 4,
    DiceFace.K: 5,
    DiceFace.ACE: 6
}

class PlayType(int, Enum):
    POINTS = 1
    PAIR = 2
    KIRIKI = 3

class Play(BaseModel):
    dice1: DiceFace
    dice2: DiceFace

    @property
    def value1(self) -> int:
        return DICE_VALUES[self.dice1]

    @property
    def value2(self) -> int:
        return DICE_VALUES[self.dice2]

    @property
    def play_type(self) -> PlayType:
        if (self.dice1 == DiceFace.REDS and self.dice2 == DiceFace.BLACKS) or \
           (self.dice1 == DiceFace.BLACKS and self.dice2 == DiceFace.REDS):
            return PlayType.KIRIKI
        if self.dice1 == self.dice2:
            return PlayType.PAIR
        return PlayType.POINTS

    @property
    def points(self) -> int:
        return self.value1 + self.value2

    def is_greater_than(self, other: 'Play') -> bool:
        if self.play_type != other.play_type:
            return self.play_type > other.play_type
        
        if self.play_type == PlayType.KIRIKI:
            return False # Cannot be greater than another Kiriki
        
        if self.play_type == PlayType.PAIR:
            return self.value1 > other.value1

        # Points
        return self.points > other.points

    def is_greater_or_equal_to(self, other: 'Play') -> bool:
        if self.is_greater_than(other):
            return True
            
        # Equal case
        if self.play_type == other.play_type:
            if self.play_type == PlayType.KIRIKI:
                return True
            if self.play_type == PlayType.PAIR:
                return self.value1 == other.value1
            if self.play_type == PlayType.POINTS:
                 return self.points == other.points
        return False

    def __str__(self):
        return f"{self.dice1.value}-{self.dice2.value}"
        
    @staticmethod
    def roll() -> 'Play':
        faces = list(DiceFace)
        return Play(dice1=random.choice(faces), dice2=random.choice(faces))

class Player(BaseModel):
    id: str
    name: str # e.g. P1, P2
    lives: int = 3
    connected: bool = True

class GameState(str, Enum):
    WAITING_FOR_PLAYERS = "WAITING"
    PLAYING_ROLL = "ROLL"
    PLAYING_DECLARE = "DECLARE"
    PLAYING_REACT = "REACT"
    REVEALING = "REVEALING"
    LIFE_LOST = "LIFE_LOST"
    KIRIKI_PUNISHMENT = "KIRIKI_PUNISHMENT"
    GAME_OVER = "GAME_OVER"

class Room(BaseModel):
    id: str
    players: List[Player] = []
    state: GameState = GameState.WAITING_FOR_PLAYERS
    current_turn_index: int = 0
    
    # Game state variables
    current_real_play: Optional[Play] = None
    current_declared_play: Optional[Play] = None
    last_declared_play: Optional[Play] = None # Tracks the exact play that the current rolling player needs to beat
    last_player_id: Optional[str] = None
    
    # Kiriki specific
    kiriki_target_id: Optional[str] = None
    kiriki_attacker_id: Optional[str] = None
    kiriki_attempts_left: int = 0
    kiriki_pairs_rolled: int = 0

    def add_player(self, player_id: str, name: str) -> Player:
        p = Player(id=player_id, name=name)
        self.players.append(p)
        return p

    def get_player(self, player_id: str) -> Optional[Player]:
        for p in self.players:
            if p.id == player_id:
                return p
        return None

    def remove_player(self, player_id: str):
        idx = -1
        for i, p in enumerate(self.players):
            if p.id == player_id:
                idx = i
                break
        
        if idx == -1: return

        was_current_turn = (self.state != GameState.WAITING_FOR_PLAYERS and idx == self.current_turn_index)
        was_kiriki_involved = (self.kiriki_target_id == player_id or self.kiriki_attacker_id == player_id)

        self.players.pop(idx)

        # Game Over if less than 2 players alive and game started
        if not self.players or len([p for p in self.players if p.lives > 0]) <= 1:
            if self.state != GameState.WAITING_FOR_PLAYERS:
                self.state = GameState.GAME_OVER
            return

        # Shift turns logically
        if self.state != GameState.WAITING_FOR_PLAYERS:
            if idx < self.current_turn_index:
                self.current_turn_index -= 1
            if self.current_turn_index >= len(self.players):
                self.current_turn_index = 0
                
            # If the current_turn_index landed on an eliminated player, advance to a living one
            while self.players and self.players[self.current_turn_index].lives <= 0:
                self.current_turn_index = (self.current_turn_index + 1) % len(self.players)

        # Skip turn and reset state for the next player if active player left
        if self.state != GameState.WAITING_FOR_PLAYERS:
            if was_current_turn or was_kiriki_involved:
                self.current_real_play = None
                self.current_declared_play = None
                self.last_declared_play = None
                self.last_player_id = None
                self.kiriki_target_id = None
                self.kiriki_attacker_id = None
                self.state = GameState.PLAYING_ROLL

    def update_turn(self, next_id: Optional[str] = None):
        if next_id:
            for i, p in enumerate(self.players):
                if p.id == next_id:
                    self.current_turn_index = i
                    break
            
            # If the forced player is dead, pass turn to the next alive person
            if self.players and self.players[self.current_turn_index].lives <= 0:
                self.update_turn(next_id=None)
                return
        else:
            if not self.players: return
            while True:
                self.current_turn_index = (self.current_turn_index + 1) % len(self.players)
                if self.players[self.current_turn_index].lives > 0:
                    break

    @property
    def current_player(self) -> Player:
        return self.players[self.current_turn_index]
        
    def alive_players_count(self) -> int:
        return sum(1 for p in self.players if p.lives > 0)

    def check_game_over(self):
        if self.alive_players_count() <= 1:
            self.state = GameState.GAME_OVER

    def reset_round(self, loser_id: str, skip_turn: bool = False):
        self.current_real_play = None
        self.current_declared_play = None
        self.last_declared_play = None
        self.last_player_id = None
        self.kiriki_target_id = None
        self.kiriki_attacker_id = None
        self.kiriki_attempts_left = 0
        self.kiriki_pairs_rolled = 0
        
        self.check_game_over()
        if self.state != GameState.GAME_OVER:
            self.state = GameState.PLAYING_ROLL
            # Loser starts
            self.update_turn(loser_id)
            if skip_turn:
                self.update_turn()
