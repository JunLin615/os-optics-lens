# Repository rules for agents

## Purpose

Help an agent find a usable optical design starting point or reference structure and continue work with a native `os-optics` Project. Optimize the repository layout, index, retrieval, help and contribution path for agent use. Spend context on the optical facts and operations needed for the next decision.

## Corpus and evidence

- The native `os-optics` Project is the authoritative prescription. Preserve geometry, units, configuration relationships and the material records or references needed to interpret it. Use the engine's native representation for each.
- Keep a native Project and compact retrieval metadata per record. Give each record a stable ID and revision. Preserve meaningful configurations and variants, and identify the design family when known.
- Use values defined by, measured on or computed from the committed Project for search, ranking and comparison. Identify their units, configuration and relevant conditions. Leave a requested fact unconfirmed when the Project has no comparable value.
- Record the direct source identified by the contributor, such as the library and file used for a conversion, the example used for a direct transcription, or the contributor for an original design. Keep applicable attribution and redistribution terms. A direct source is sufficient.
- Keep supporting data only when it makes a Project fact usable. Put transcription comparisons, intake reports, preparation scripts and raw assets in the contribution workspace, upstream, or in the batch discussion. Include required license notices in distributions.

## Retrieval requirements for the future package

- Ship a searchable collection and the retriever together. After installation, queries should run locally with the package's index and data. Use an index that can be generated from the source records. Keep each record independently maintainable so contributors can work in parallel.
- Return compact, structured summaries with the ID, revision, configuration and facts relevant to the query. Report shortfalls and unconfirmed facts for the requested constraints. Omit unrelated gaps.
- Support narrowing, comparison and inspection of selected Project facts, surfaces and configurations. Keep provenance behind an explicit request, including for ordinary inspection and Project handoff. Exclude source optical values and intake comparisons from the search index, matching, ranking and routine responses.
- Allow direct retrieval by record ID and include the resolved revision. Preserve the distinction between a specification match and a starting point for refinement.
- Bound total output for a multi-query request as well as output for a single query. Provide deterministic ordering within a collection snapshot and a way to continue through omitted results. Keep details accessible on demand.
- Retrieval hands off an editable native Project. `os-optics` evaluates it. `os-optics-kit` can teach or apply the relevant downstream workflows.

## Changes

Use English by default for repository documentation, agent-facing text, issues and pull requests. Preserve source titles and original identifiers in citations.

Follow the architecture and field semantics in [ARCHITECTURE.md](ARCHITECTURE.md) and the workflow in [CONTRIBUTING.md](CONTRIBUTING.md). Use a focused pull request linked to the relevant issue when one exists. Keep shared decisions in issues or repository rules so later contributors can find them.
