"""Optional integrations between the SDK and third-party agent runtimes.

Each module here is imported only by the caller that wants it and depends on
its runtime only through an optional extra, so the SDK's required dependency
set stays what ``pyproject.toml`` declares. ``nemo_relay`` is the first: an
exporter and policy gate for NVIDIA's NeMo Relay (``pip install
unitares-sdk[nemo-relay]``).
"""
