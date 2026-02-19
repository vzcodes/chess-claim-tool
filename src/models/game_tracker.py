"""
Chess Claim Tool: Game Tracker

Tracks the state of all games being scanned, including move count,
last update time, and game status.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import Lock
from typing import Dict, Optional, List
from chess.pgn import Game


class GameStatus(Enum):
    ACTIVE = "Active"
    FINISHED = "Finished"
    INVALID = "Invalid"


@dataclass
class TrackedGame:
    """Represents a tracked game's state."""
    players: str
    board: str
    move_count: int
    last_update: datetime
    status: GameStatus
    result: str = "*"
    last_move: str = ""
    claims: List[str] = field(default_factory=list)
    has_error: bool = False
    error_at_move: Optional[int] = None

    def time_since_update(self) -> str:
        """Returns formatted string of time since last update."""
        delta = datetime.now() - self.last_update
        total_seconds = int(delta.total_seconds())
        
        if total_seconds < 60:
            return f"{total_seconds}s"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            return f"{minutes}m"
        else:
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            return f"{hours}h {minutes}m"


class GameTracker:
    """
    Thread-safe tracker for all games being scanned across scan cycles.
    
    Attributes:
        games: Dictionary mapping player string to TrackedGame
        _lock: Threading lock for thread-safe access.
    """
    
    def __init__(self):
        self.games: Dict[str, TrackedGame] = {}
        self._lock = Lock()
    
    def update_game(self, game: Game, players: str, board: str, 
                    move_count: int, last_move: str, 
                    has_error: bool = False, error_at_move: Optional[int] = None) -> TrackedGame:
        """
        Update or create a tracked game entry. Thread-safe.
        
        Returns the TrackedGame (new or updated).
        """
        result = game.headers.get("Result", "*")
        
        if result == "*":
            status = GameStatus.ACTIVE
        else:
            status = GameStatus.FINISHED
        
        if has_error:
            status = GameStatus.INVALID
        
        with self._lock:
            if players in self.games:
                existing = self.games[players]
                # Only update timestamp if move count changed
                if move_count != existing.move_count:
                    existing.last_update = datetime.now()
                existing.move_count = move_count
                existing.status = status
                existing.result = result
                existing.last_move = last_move
                existing.has_error = has_error
                existing.error_at_move = error_at_move
                return existing
            else:
                tracked = TrackedGame(
                    players=players,
                    board=board,
                    move_count=move_count,
                    last_update=datetime.now(),
                    status=status,
                    result=result,
                    last_move=last_move,
                    has_error=has_error,
                    error_at_move=error_at_move
                )
                self.games[players] = tracked
                return tracked
    
    def add_claim_to_game(self, players: str, claim_type: str) -> None:
        """Add a claim to a tracked game. Thread-safe."""
        with self._lock:
            if players in self.games:
                if claim_type not in self.games[players].claims:
                    self.games[players].claims.append(claim_type)
    
    def get_all_games(self) -> List[TrackedGame]:
        """Returns list of all tracked games."""
        return list(self.games.values())
    
    def clear(self) -> None:
        """Clear all tracked games."""
        self.games.clear()
