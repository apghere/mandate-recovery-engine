# Diagrams

`architecture.mmd` is the same Mermaid source embedded in
[`README.md`](../../README.md#5-architecture) (Section 5, "Architecture"),
exported here as a standalone file per docs P.1's file tree. It is copied
by hand, not generated — if you edit the diagram, update both places.

GitHub renders `.mmd`/Mermaid fenced blocks natively; there is no
separate rendering step or build tool involved. To render a PNG/SVG
locally (e.g. for the submission video or a slide), paste the contents
into the [Mermaid Live Editor](https://mermaid.live) and export — no
Node/mermaid-cli toolchain is installed in this project's environment,
and adding one solely to produce a static image was judged not worth the
dependency this close to the deadline.
