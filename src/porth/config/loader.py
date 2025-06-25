"""Configuration loading and hot-reload functionality."""

import logging
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

if TYPE_CHECKING:
    from watchdog.observers import Observer

from porth.config.settings import Settings

logger = logging.getLogger(__name__)


class ConfigFileHandler(FileSystemEventHandler):
    """File system event handler for config file changes."""

    def __init__(self, config_file: Path, reload_callback: Callable[[Settings], None]):
        self.config_file = config_file
        self.reload_callback = reload_callback

    def on_modified(self, event):
        """Handle file modification events."""
        if not event.is_directory and Path(str(event.src_path)) == self.config_file:
            logger.info(f'Config file {self.config_file} modified, reloading...')
            try:
                new_settings = Settings.load_from_file(str(self.config_file))
                self.reload_callback(new_settings)
                logger.info('Configuration reloaded successfully')
            except Exception as e:
                logger.error(f'Failed to reload configuration: {e}')


class ConfigLoader:
    """Configuration loader with hot-reload capability."""

    def __init__(self, config_file: Optional[str] = None):
        self.config_file = Path(config_file) if config_file else None
        self.observer: Optional[Observer] = None  # type: ignore
        self.reload_callbacks: list[Callable[[Settings], None]] = []

    def load_settings(self) -> Settings:
        """Load settings from file or environment."""
        if self.config_file and self.config_file.exists():
            return Settings.load_from_file(str(self.config_file))
        return Settings()

    def add_reload_callback(self, callback: Callable[[Settings], None]) -> None:
        """Add a callback to be called when configuration is reloaded."""
        self.reload_callbacks.append(callback)

    def start_watching(self) -> None:
        """Start watching config file for changes."""
        if not self.config_file or not self.config_file.exists():
            logger.warning('No config file to watch for changes')
            return

        def reload_handler(new_settings: Settings):
            for callback in self.reload_callbacks:
                try:
                    callback(new_settings)
                except Exception as e:
                    logger.error(f'Error in reload callback: {e}')

        event_handler = ConfigFileHandler(self.config_file, reload_handler)
        self.observer = Observer()
        if self.observer is not None:
            self.observer.schedule(
                event_handler, str(self.config_file.parent), recursive=False
            )
            self.observer.start()
            logger.info(f'Started watching {self.config_file} for changes')
        else:
            logger.error('Failed to create Observer for config file watching')

    def stop_watching(self) -> None:
        """Stop watching config file for changes."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            logger.info('Stopped watching config file')
