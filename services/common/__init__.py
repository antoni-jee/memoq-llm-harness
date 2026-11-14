"""Code shared by more than one backend service.

Only helpers that two services genuinely use live here: database URL parsing
and connections (:mod:`services.common.db`) and the SQL migration runner
(:mod:`services.common.migrations`). Each service keeps its own environment
variable names, schema, migrations and advisory-lock namespace.
"""
