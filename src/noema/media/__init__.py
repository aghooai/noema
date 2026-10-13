"""noema.media (spec-01 §1, spec-07): the upload/inline pipeline and the media views.

media-pipeline modules: `intake` (type map, pre-check, multipart), `quarantine` (S3 layout and streaming puts),
`pipeline` (`MediaFileService`, status machine, FileObject mapping, file events), `worker` (`media.process` /
`file.purge` handlers), `sandbox/` (runners, sandboxd, manifest validation) and `extractors/` (run only inside the
sandbox; the only modules allowed to import the media libraries). media-negotiation owns `negotiation`, `budget`
and `views`. Nothing here is imported eagerly: the API and worker images have no media libraries.
"""
