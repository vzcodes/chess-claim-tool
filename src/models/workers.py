"""
Chess Claim Tool: workers

Copyright (C) 2022 Serntedakis Athanasios <thanserd@hotmail.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
from __future__ import annotations

import os.path
from threading import Thread
from typing import List, TYPE_CHECKING, Dict

from PyQt6.QtCore import QRunnable, QThread, pyqtSignal
from chess.pgn import read_game
from src.helpers import get_appdata_path, Status
from src.models.claims import get_players, Claims
from src.models.download import check_download, download_pgn

if TYPE_CHECKING:
    from src.controllers import SourceDialogController
    from src.views.dialog_view import SourceHBox
    from src.models.game_tracker import GameTracker
    from threading import Event, Lock
    from PyQt6.QtGui import QAction


class CheckDownload(QRunnable):
    """ Checks if a web source is valid.
    Attributes:
        controller: Object of SourceDialogController.
        source: The web source to be checked.
        download_id: A unique download id
    """
    __slots__ = ["controller", "source", "download_id"]

    def __init__(self, controller: SourceDialogController, source: SourceHBox, download_id: int):
        super().__init__()
        self.controller = controller
        self.source = source
        self.download_id = download_id

    def run(self):
        url = self.source.get_value()
        if check_download(url):
            self.source.set_status(Status.OK)
            if url not in self.controller.downloads:
                self.controller.add_valid_url(url, self.download_id)
        else:
            self.source.set_status(Status.ERROR)


class DownloadGames(QThread):
    """ Downloads a list of sources from the web.

    Attributes:
        downloads: The list of urls to download.
        stop_event: A stop signal that is emitted to stop this thread execution
    """
    status_signal = pyqtSignal(Status)
    INTERVAL = 4
    __slots__ = ["downloads", "stop_event", "app_path"]

    def __init__(self, downloads: Dict[str, str], stop_event: Event = None):
        super().__init__()
        self.downloads = downloads
        self.stop_event = stop_event
        self.app_path = get_appdata_path()

    def run(self) -> None:
        if not self.stop_event:
            return self.download_pgns()

        while not self.stop_event.is_set():
            self.download_pgns()
            self.stop_event.wait(self.INTERVAL)

    def download_pgns(self):
        for url in self.downloads:
            status = Status.OK

            data = download_pgn(url)
            if not data:
                status = Status.ERROR
            self.status_signal.emit(status)

            filename = self.downloads[url]
            try:
                with open(filename, "wb") as file:
                    file.write(data)
            except (FileNotFoundError, TypeError):
                self.status_signal.emit(Status.ERROR)
                continue


class ScanFile(QThread):
    """ Scans a single PGN file directly for claims.
    
    This replaces the old Scan + MakePgn architecture by reading
    each source file directly, eliminating the copy step.

    Attributes:
        filename: The path of the PGN file to scan.
        claims: An Object of Claims Class (thread-safe).
        game_tracker: Tracks all games and their state (thread-safe).
        live_pgn_option: The checkbox object on the menu.
        stop_event: A stop signal that is emitted to stop this thread execution.
    """
    __slots__ = ["filename", "claims", "game_tracker", "live_pgn_option", "stop_event"]

    add_entry_signal = pyqtSignal(tuple)
    status_signal = pyqtSignal(Status)
    games_count_signal = pyqtSignal(str, int)  # (filepath, count)
    game_update_signal = pyqtSignal(str)  # Emits players string when a game is updated
    INTERVAL = 2  # Faster polling since we're not waiting for MakePgn

    def __init__(self, claims: Claims, game_tracker: GameTracker, filename: str, live_pgn_option: QAction, stop_event: Event):
        super().__init__()
        self.filename = filename
        self.claims = claims
        self.game_tracker = game_tracker
        self.live_pgn_option = live_pgn_option
        self.stop_event = stop_event

    def run(self):
        last_size = 0

        while not self.stop_event.is_set():
            try:
                size_of_pgn = os.path.getsize(self.filename)
            except FileNotFoundError:
                size_of_pgn = 0

            if self.is_file_updated(last_size, size_of_pgn):
                self.status_signal.emit(Status.ACTIVE)
                self.check_pgn()

            self.status_signal.emit(Status.WAIT)
            last_size = size_of_pgn

            self.stop_event.wait(self.INTERVAL)

    def check_pgn(self):
        games_in_scan = 0

        try:
            with open(self.filename) as pgn:
                while not self.stop_event.is_set():
                    game = read_game(pgn)

                    if not game:
                        break

                    games_in_scan += 1
                    players = get_players(game)
                    board = self.claims.get_board_number(game)

                    # Count moves and get last move, handling potential errors
                    move_count = 0
                    last_move = ""
                    has_error = False
                    error_at_move = None

                    try:
                        game_board = game.board()
                        for move in game.mainline_moves():
                            move_count += 1
                            try:
                                san = game_board.san(move)
                                game_board.push(move)
                                last_move = self.claims.get_printable_move(move_count, san)
                            except Exception:
                                has_error = True
                                error_at_move = move_count
                                last_move = f"Error at move {move_count}"
                                break
                    except Exception:
                        has_error = True
                        last_move = "Parse error"

                    # Update game tracker for ALL games (thread-safe)
                    tracked_game = self.game_tracker.update_game(
                        game, players, board, move_count, last_move, has_error, error_at_move
                    )
                    self.game_update_signal.emit(players)

                    # Skip claim checking only if live_pgn is checked AND game is finished
                    if self.live_pgn_option.isChecked() and game.headers.get("Result", "*") != "*":
                        continue

                    if self.claims.is_in_dont_check(players):
                        continue

                    if has_error:
                        continue  # Skip claim checking for games with errors

                    entries = self.claims.check_game(game)
                    for entry in entries:
                        self.add_entry_signal.emit(entry)
                        # Also track claims in the game tracker
                        self.game_tracker.add_claim_to_game(players, entry[0].value)
                        self.game_update_signal.emit(players)
        except FileNotFoundError:
            pass  # File doesn't exist yet, will retry on next interval

        self.games_count_signal.emit(self.filename, games_in_scan)

    @staticmethod
    def is_file_updated(last_size: int, current_size: int):
        return current_size != 0 and last_size != current_size


# Keep legacy Scan class for backwards compatibility if needed
class Scan(QThread):
    """ [DEPRECATED] Use ScanFile instead.
    
    Continuously looks for a new games.pgn to scan. It creates another thread
    to check the new pgn while it updates the GUI(claimsTable) with new entries.

    Attributes:
        filename: The path of the combined pgn file.
        claims: An Object of Claims Class.
        game_tracker: Tracks all games and their state.
        lock: The fileLock for the games.pgn between CheckPgn and MakePgn threads.
        live_pgn_option: The checkbox object on the menu.
        stop_event: A stop signal that is emitted to stop this thread execution
    """
    __slots__ = ["filename", "claims", "game_tracker", "lock", "live_pgn_option", "stop_event"]

    add_entry_signal = pyqtSignal(tuple)
    status_signal = pyqtSignal(Status)
    games_count_signal = pyqtSignal(int)
    game_update_signal = pyqtSignal(str)  # Emits players string when a game is updated
    INTERVAL = 4

    def __init__(self, claims: Claims, game_tracker: GameTracker, filename: str, lock: Lock, live_pgn_option: QAction, stop_event: Event):
        super().__init__()
        self.filename = filename
        self.claims = claims
        self.game_tracker = game_tracker
        self.lock = lock
        self.live_pgn_option = live_pgn_option
        self.stop_event = stop_event

    def run(self):
        last_size = 0

        while not self.stop_event.is_set():
            try:
                size_of_pgn = os.path.getsize(self.filename)
            except FileNotFoundError:
                size_of_pgn = 0

            if self.is_file_updated(last_size, size_of_pgn):
                self.status_signal.emit(Status.ACTIVE)
                self.check_pgn()

            self.status_signal.emit(Status.WAIT)
            last_size = size_of_pgn

            self.stop_event.wait(self.INTERVAL)

    def check_pgn(self):
        self.lock.acquire()
        games_in_scan = 0

        with open(self.filename) as pgn:
            while not self.stop_event.is_set():
                game = read_game(pgn)

                if not game:
                    break

                games_in_scan += 1
                players = get_players(game)
                board = self.claims.get_board_number(game)

                # Count moves and get last move, handling potential errors
                move_count = 0
                last_move = ""
                has_error = False
                error_at_move = None

                try:
                    game_board = game.board()
                    for move in game.mainline_moves():
                        move_count += 1
                        try:
                            san = game_board.san(move)
                            game_board.push(move)
                            last_move = self.claims.get_printable_move(move_count, san)
                        except Exception:
                            has_error = True
                            error_at_move = move_count
                            last_move = f"Error at move {move_count}"
                            break
                except Exception:
                    has_error = True
                    last_move = "Parse error"

                # Update game tracker for ALL games
                tracked_game = self.game_tracker.update_game(
                    game, players, board, move_count, last_move, has_error, error_at_move
                )
                self.game_update_signal.emit(players)

                # Skip claim checking only if live_pgn is checked AND game is finished
                # (we now process finished games for claims too)
                if self.live_pgn_option.isChecked() and game.headers.get("Result", "*") != "*":
                    continue

                if players in self.claims.dont_check:
                    continue

                if has_error:
                    continue  # Skip claim checking for games with errors

                entries = self.claims.check_game(game)
                for entry in entries:
                    self.add_entry_signal.emit(entry)
                    # Also track claims in the game tracker
                    self.game_tracker.add_claim_to_game(players, entry[0].value)
                    self.game_update_signal.emit(players)

        self.games_count_signal.emit(games_in_scan)
        self.lock.release()

    @staticmethod
    def is_file_updated(last_size: int, current_size: int):
        return current_size != 0 and last_size != current_size


class Stop(QThread):
    """ Stops all the other running Threads(downloadWorker, scan_workers)
    and resets the model for the next scan.

    Attributes:
        stop_event: The stop event that can signal the termination of threads
        download_worker: Running thread, object of Download Class.
        scan_workers: List of running scan threads (one per file).
        make_pgn_worker: [DEPRECATED] Running thread, object of makePgn Class.
    """
    enable_signal = pyqtSignal()
    disable_signal = pyqtSignal()

    __slots__ = ["stop_event", "make_pgn_worker", "scan_workers", "download_worker"]

    def __init__(self, stop_event: Event, scan_workers: List[QThread],
                 download_worker: QThread = None, make_pgn_worker: Thread = None):
        super().__init__()
        self.stop_event = stop_event
        self.download_worker = download_worker
        self.scan_workers = scan_workers if isinstance(scan_workers, list) else [scan_workers]
        self.make_pgn_worker = make_pgn_worker

    def run(self):
        self.disable_signal.emit()
        self.stop_event.set()

        if self.download_worker:
            self.download_worker.wait()
        
        # Wait for all scan workers to finish
        for scan_worker in self.scan_workers:
            if scan_worker:
                scan_worker.wait()
        
        # Legacy support for MakePgn if still used
        if self.make_pgn_worker:
            self.make_pgn_worker.join()

        self.enable_signal.emit()


class MakePgn(Thread):
    """ Makes a combined pgn of all the sources available (using the filePathList).
    The thread execution can be stopped by "setting" the event (`stop_event.set()`).
    If the event is not provided the thread will only execute once.

    Attributes:
        filepaths: A list that contains all the files path(or url) which are valid.
        stop_event: The event that is responsible for the execution of the thread.
        lock: The fileLock for the games.pgn between CheckPgn and MakePgn threads.
    """
    INTERVAL = 4
    __slots__ = ["filepaths", "stop_event", "is_running", "lock", "daemon"]

    def __init__(self, filepaths: List[str], stop_event: Event = None, lock: Lock = None):
        super().__init__()
        self.filepaths = filepaths
        self.lock = lock
        self.stop_event = stop_event
        self.daemon = True

        app_path = get_appdata_path()
        self.filename = os.path.join(app_path, "games.pgn")

    def run(self) -> None:
        if not self.stop_event:
            return self.make_pgn()

        while not self.stop_event.is_set():
            self.make_pgn()
            self.stop_event.wait(self.INTERVAL)

    def make_pgn(self):
        data = bytes()
        for filepath in self.filepaths:
            try:
                with open(filepath, "rb") as in_file:
                    data += "\n\n".encode("utf-8") + in_file.read()
            except FileNotFoundError:
                continue

        self.lock_file()
        with open(self.filename, "wb") as file:
            file.write(data)
        self.release_file()

    def lock_file(self):
        if self.lock:
            self.lock.acquire()

    def release_file(self):
        if self.lock:
            self.lock.release()
