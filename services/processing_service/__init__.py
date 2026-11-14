"""processing-service: stateless prompt building and model calls.

Builds the system prompt with :func:`memoq_harness.prompts.build_batch_prompt`
(the same assembly the Streamlit app uses) and runs either the deterministic
offline ``stub`` engine (default) or a real engine configured on the server
through :class:`memoq_harness.llm.LLMClient`. It has no database, reads no
document files and calls no other service.
"""

SERVICE_NAME = "processing-service"
