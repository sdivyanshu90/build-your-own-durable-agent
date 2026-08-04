# Sample notification service

This fixture sends messages through a transport. Transient transport errors are retried
using a fixed delay. The intentionally missing feature is a configurable retry limit;
the current implementation uses a hard-coded limit to avoid hanging forever.

Configuration is loaded by `sample_service.config.ServiceConfig`. Callers that do not
provide new settings must retain the historical three-attempt behavior.
