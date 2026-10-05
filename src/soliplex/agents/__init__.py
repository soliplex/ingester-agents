#
__version__ = "0.1.0"


class ValidationError(Exception):
    def __init__(self, config):
        super().__init__(f"Invalid config: {config}")


class EmptyComponentError(Exception):
    """A component with ``error_on_empty`` set found nothing to ingest.

    Raised by the manifest runner, so the component counts as failed and the
    stale-document clean-up is skipped instead of deleting the whole source.
    """

    def __init__(self, component: str, component_type: str, source: str, inventory: int, not_found: int):
        self.component = component
        self.component_type = component_type
        self.source = source
        self.inventory = inventory
        self.not_found = not_found
        super().__init__(
            f"Component '{component}' ({component_type}) returned no items (after extension filtering) "
            f"and has error_on_empty set; skipping stale-document removal for source '{source}' "
            f"(inventory={inventory}, not_found={not_found})"
        )
