"""Use the operating system's trust store for outbound HTTPS requests."""

import truststore

truststore.inject_into_ssl()
