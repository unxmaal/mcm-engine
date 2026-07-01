# Rules categorization — managing sprawl as mcm-engine grows

Notes for reining in rule-file duplication and category sprawl without
losing the ease that makes contributors actually file rules in the first
place.

## Observed state

Snapshot from `~/projects/gitlab/commons/agent_tricks/rules` (a real shared
rules tree consumed by multiple projects via `rules_path`):

- ~110 rule files, 36 categories.
- `add_rule` creates files at `rules/{category}/{slug}.md`, slug from
  title. Duplicate-detection is title-string-equality only.
- No category vocabulary — any string a contributor passes becomes a
  directory.
- Visible sprawl pairs already in the tree: `aws` / `corning-aws`,
  `sso` / `corning-sso`, `gitlab` / `gitlab-ci` / `gitlab-runner`,
  `ci-cd` (1 file) parallel to `gitlab-ci` (5 files),
  `spec-generation` / `specstack` / `retrodoc` (adjacent topics).
- One empty category (`tctv`) accumulating.
- Density imbalance: `sre` has 27 files in 8 sub-dirs (the only nested
  category, and the right pattern); several categories have 1 file.

Two failure modes worth solving:
1. **Category sprawl** — every new contributor invents a new directory.
2. **Near-duplicate rules** — same idea, multiple titles, no link
   between them.

## Recommendations, ranked by ROI

### 1. Controlled-vocabulary categories — highest impact, lowest friction

Maintain `rules/_meta/categories.yaml` listing approved categories with a
one-line description and an owner:

```yaml
aws:
  description: AWS service mechanics and gotchas. Cloud-agnostic stuff goes in `infrastructure`.
  owner: dodde
gitlab-ci:
  description: GitLab CI pipeline patterns, runner config, components.
  owner: dodde
  subcategories: [components, runners, jobs]   # optional — only if needed
sre/observability:
  description: Logging, metrics, tracing.
  owner: paul
```

Two checks wire it up:

- `add_rule` in mcm-engine: if `category` isn't in the vocabulary, return
  a soft warning that lists the closest matches and accepts a
  `--force-new-category` override. Doesn't block, just guides.
- A CI job over the rules repo validates every rule's frontmatter
  `Category:` is in the vocabulary. PRs that introduce a new category
  must also touch `categories.yaml` — implicit review gate.

Bootstrap by collapsing the existing 36 → ~15: `corning-aws` folds into
`aws`, `corning-sso` into `sso`, `gitlab-runner` into `gitlab-ci`,
`ci-cd` either disappears or absorbs `gitlab-ci`, `tctv` deletes (empty),
`spec-generation` / `specstack` / `retrodoc` either consolidate or get
clear scope statements.

Solves sprawl without blocking writes. Cost: one yaml file + one lint
job + one bootstrap rename pass.

### 2. Suggest-related on `add_rule` — solves duplication at write time

In `add_rule`, after the title-equality dedup check, run a cheap
keyword-overlap search across existing rules (FTS5 index already
exists). If any rule has ≥3 keyword overlaps OR a fuzzy title match
(Levenshtein < 5), return a suggestion in the response:

```
Created rule: "GitLab CI component standalone-job pattern"
Possibly related (consider linking via link_knowledge or merging):
  - rules/gitlab-ci/building-gitlab-ci-components.md  (5 keyword overlap)
  - rules/gitlab-ci/commit-back-from-ci-via-job-token.md  (3 keyword overlap)
```

Doesn't block creation. Doesn't slow `add_rule` perceptibly. Surfaces
the question at exactly the right moment — when the contributor is
still in the headspace of "is this new or an extension of an existing
rule?". `link_knowledge` already supports the supersedes / related
edges; this just nudges contributors to use them.

### 3. Periodic agent-driven merge sweeps — safety net

Quarterly (or on demand), an agent reads the full rule index, clusters
by FTS5 similarity, surfaces top-N candidate-merge clusters with a
one-line description per cluster. Human approves, agent does the merge
(file rewrite via `add_rule` overwrite + `link_knowledge supersedes`).

Cheap to implement — a standalone script that consumes the FTS5 db and
emits a markdown report. Doesn't need to live inside mcm-engine.

## Optional / further-out

- **Tags as frontmatter** (`tags: [gitlab, ci, corning]`) for
  cross-cutting topics that want to live in one directory but be findable
  from many lenses. Additive; search becomes (category OR tag) match.
- **Stale auto-archive**: rules >180d with zero hits move to
  `rules/_archive/` so they stop polluting search. mcm already detects
  `[STALE]`; this is the action layer.
- **Rule shape lint**: enforce sections (Symptom / Why / How to fix /
  See-also). Reduces "narrative blob" rules that are hard to dedupe.
  Higher value but more invasive — would force a rewrite pass on
  existing rules.
- **Promotion gate**: only promote DB knowledge to rules after N hits
  ("things proven useful," not "things someone tried once").
  `promote_to_rule` exists; gate it on a hit-count threshold.

## Suggested implementation order

1. Draft a starter `categories.yaml` from the existing 36 categories.
   That alone forces the consolidation conversation.
2. Add the soft-warning path to `add_rule`. One file change in
   `tools/rules.py`, no schema change.
3. Add the related-rules suggestion to the `add_rule` response. Reuses
   the FTS5 index already present.
4. Wait a quarter, then write the merge-sweep script.

The first three are low-cost and stack: each one improves the situation
without depending on the others landing.
