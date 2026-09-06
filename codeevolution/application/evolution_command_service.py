"""Evolution analysis command use cases."""


class EvolutionCommandService:
    """Own the analysis engine lifecycle for delivery adapters."""

    def __init__(self, engine):
        self.engine = engine

    @classmethod
    def from_config(cls, config):
        from ..engine import EvolutionEngine

        return cls(EvolutionEngine(config))

    def backfill(self, progress_callback=None) -> dict:
        self.engine.backfill(progress_callback=progress_callback)
        return self.engine.store.get_stats()

    def update(self) -> dict:
        self.engine.update()
        return self.engine.store.get_stats()

    def close(self):
        self.engine.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
