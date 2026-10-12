from __future__ import annotations


class CatalogBridgeError(Exception):
    pass


class ConfigError(CatalogBridgeError):
    pass


class MissingAPIKeyError(ConfigError):
    def __init__(self) -> None:
        super().__init__(
            "NEBIUS_API_KEY is not set. Copy .env.example to .env and add your "
            "Nebius Token Factory key."
        )


class ConnectorError(CatalogBridgeError):
    pass


class UnsupportedSourceError(ConnectorError):
    pass


class ConnectorHTTPError(ConnectorError):
    def __init__(self, url: str, status_code: int, detail: str = "") -> None:
        self.url = url
        self.status_code = status_code
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{url} returned HTTP {status_code}{suffix}")


class StoreNotShopifyError(ConnectorError):
    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"{url} does not serve a Shopify products feed")


class StorePasswordProtectedError(ConnectorError):
    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"{url} is password protected, its catalog is not public")


class NormalizationError(CatalogBridgeError):
    def __init__(self, source_id: str, reason: str) -> None:
        self.source_id = source_id
        self.reason = reason
        super().__init__(f"cannot normalize record {source_id or '<no id>'}: {reason}")


class TargetSpecError(CatalogBridgeError):
    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"invalid target spec {path}: {reason}")


class UnsupportedTargetFormatError(CatalogBridgeError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"no loader for output format {name}")


class LLMError(CatalogBridgeError):
    pass


class LLMTransportError(LLMError):
    pass


class PromptNotFoundError(LLMError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"no prompt file named {name}.md")


class SchemaValidationError(LLMError):
    def __init__(self, model_name: str, errors: str, raw_output: str) -> None:
        self.model_name = model_name
        self.errors = errors
        self.raw_output = raw_output
        super().__init__(f"model output did not validate against {model_name}: {errors}")
