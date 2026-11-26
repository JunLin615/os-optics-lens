# Contributing

Individuals, research groups and companies can contribute a single optical prescription, a related design family or an independently maintainable collection. Agents can help prepare and check submissions. The contributor is responsible for the content and evidence in the pull request.

## Coordinate the work

1. Search the issue tracker for the collection, design family or tool change. Join an existing discussion when one covers the work. Open an issue for a new collection, bulk import, or change to the record format or retrieval interface. Include the scope, source, expected coverage and how the result can be checked.
2. Fork the repository and work on a focused branch. A single record or correction using an agreed format can come in a pull request directly. For a collection or bulk import, use its issue to agree on how to partition the work and avoid duplicate conversions.
3. Open a pull request with the intended change, related issue if any, and the checks run. Respond to review in the pull request. Maintainers review and merge into `main`.

During repository setup, contributors can open a draft data pull request to review representative native Projects alongside the proposed format and validation procedure. If a format proposal is already under review, link it and identify any fields or checks the new records need. Before merge, align the submission with the accepted record format and validation procedure.

## Prepare a prescription

- Supply the editable native `os-optics` Project with the material and configuration information it needs. Open it with the stated engine version, check the applicable geometry and confirm that each submitted search value describes that Project and configuration.
- Supply compact search facts useful for choosing or using the Project. Include units, conditions and calculation basis where relevant. Use the [optical search vocabulary](SEARCH_VOCABULARY.md) for classification. Describe model assumptions where they affect a searchable value or how to use the Project.
- Name the direct source used for the submission. For a library conversion give the library and input identifier, plus the revision when available. For a direct transcription give the publication and example. For an original design name the contributor. Include applicable attribution and the basis for redistribution.
- Report intake checks and decisions in the PR or series issue. Use comparable source values as transcription checks when useful. Resolve any issue they reveal with the Project or its metadata. A source comparison is optional intake evidence. The Project's own checks and the support for its advertised values determine its retrieval claims.

## Prepare a collection or batch

- Propose a collection identity, identify its maintainers and pin the source revision or edition where possible. Describe how source record IDs map to contributed records and how future updates can be tracked.
- In the PR or series issue, report the input count, submitted records, deferrals and failures with reasons. Keep the preparation inputs and scripts available to the next contributor outside the distributed corpus. Split large submissions into independently checkable batches.
- Review existing records for duplicates. Keep the positions of one design in the same Project when native configurations can represent them. Retain distinct prescriptions where their optical parameters or structure make them useful, and group related designs by family. Generate the shared index from the individual records.

Project code and documentation use the [MIT License](LICENSE). Imported records retain their applicable attribution and license or permission. Reviewers check that the submitted files fit the corpus boundary, the source and redistribution basis are recorded, and the evidence supports each claimed result.
