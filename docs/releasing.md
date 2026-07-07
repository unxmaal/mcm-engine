# Releasing & image tags

## The model

Container tags split into **immutable identity** and **moving pointers**:

- **Immutable** (safe to deploy; never reused for a different build):
  - `:sha-<commit>` — every build gets one, tied to the exact commit.
  - `:X.Y.Z` — a release, built **once** when you tag `vX.Y.Z`.
  - the image **digest** (`@sha256:…`) — the strongest form; a tag can be
    re-pushed, a digest cannot.
- **Moving** (for humans/dev; never pin these in prod):
  - `:main` — latest build of `main`.
  - `:X.Y`, `:X`, `:latest` — release aliases that float forward as you patch.

CI (`.github/workflows/ci.yml`) produces:

| Trigger | Tags pushed |
| --- | --- |
| push to `main` | `:main`, `:sha-<commit>` |
| pull request | `:pr-<n>`, `:sha-<commit>` (built + smoke-tested, **not** pushed) |
| push tag `vX.Y.Z` | `:X.Y.Z`, `:X.Y`, `:X`, `:latest`, `:sha-<commit>` |

Key property: **`main` never rebuilds a `:X.Y.Z`.** Semver is minted exactly once,
on the release tag — so you stop overwriting the same version tag on every merge.

## Cutting a release

1. Bump `version` in `pyproject.toml`, update `CHANGELOG.md`, and (if the chart
   changed) `deploy/helm/mcm-engine/Chart.yaml`. Commit and merge to `main`.
2. Tag the merge commit and push the tag:
   ```sh
   git tag v3.6.0
   git push origin v3.6.0
   ```
   CI guards that the tag matches `pyproject.toml` (`v3.6.0` ⇒ `version = "3.6.0"`)
   and fails the release if they diverge — so bump first, tag second.
3. CI builds and pushes `:3.6.0`, `:3.6`, `:3`, `:latest`, `:sha-<commit>`.

## Deploying

Pin an immutable ref in your Helm values (or GitOps):

```yaml
image:
  repository: ghcr.io/unxmaal/mcm-engine
  tag: "3.6.0"            # a release, or a sha-<commit> for a specific main build
```

The strongest pin is the **digest** (printed in each build's job summary):

```yaml
image:
  repository: ghcr.io/unxmaal/mcm-engine
  digest: "sha256:…"      # immutable; survives a tag being re-pushed
```

At fleet scale the endgame is a GitOps controller (Argo CD / Flux image-updater)
watching the registry and bumping the pinned **digest** — the image is built once
per commit and every deploy is digest-pinned, so rollbacks are trivial. Never
deploy `:main` or `:latest` to a real environment.
