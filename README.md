# varve

`varve` is a small Python library for serial experiment orchestration with a materialized, content-addressed cache. It is intentionally thin: experiments own their output paths and file formats, while varve owns the ledger that records which stage successfully produced which durable artifacts for a given content key.

The current implementation is being built in phases from the workspace design document. Public APIs are exported from `varve.__init__` as they become available.

