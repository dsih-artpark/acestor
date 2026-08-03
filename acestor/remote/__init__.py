"""Remote-runner: offload an acestor pipeline run to a cloud instance.

The entry point is ``python -m acestor.remote`` (see :mod:`acestor.remote.cli`).
Everything cloud-specific lives behind the :class:`CloudProvider` interface in
:mod:`acestor.remote.providers.base`, so a future GCP backend slots in without
touching the runner or the CLI.
"""
